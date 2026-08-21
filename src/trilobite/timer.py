"""Session timers (sleep_until).

The ``sleep_until`` virtual tool suspends a session until a target time
(see ``doc/product/timer.md``). A suspension is a single ``sleep_until``
field in the session's ``session.json`` (epoch seconds) plus an in-memory
pending table; while suspended the session spends no tokens and its broker
sits idle (the sidebar shows a blue dot).

Design notes:

- Waking re-enters the SAME conversation: the agent appends a synthetic
  ``⏰ 定时唤醒（<now>）`` user message and starts a normal run -- the model
  sees the sleep placeholder tool result plus the wake-up message and
  continues where it left off.
- The wake-up message carries the real current time, so a wake-up that
  fires late (e.g. the server was down at the target time) still tells the
  model the truth. Wake times that fell during downtime stay armed and
  fire right after startup.
- A session whose agent is still running when its wake time arrives (long
  sibling tools from the sleeping turn) keeps its suspension armed; the
  tick retries every second until the run ends.
- The tick is a wall-clock comparison against ``time.time()``, so clock
  changes behave sensibly (target moved into the past -> fires now; moved
  into the future -> waits). One dict scan per second is negligible for a
  single-user local app.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

_log = logging.getLogger(__name__)

#: Field name inside a session's session.json while it is suspended.
SLEEP_FIELD = "sleep_until"

#: Prefix of the synthetic user message that wakes a session.
WAKE_PREFIX = "⏰ 定时唤醒"

#: A suspension must be at least this far in the future (shorter waits are
#: rejected as pointless -- the model can just continue working).
MIN_DELAY = 5.0

#: ...and at most this far (a year; "wake me in 2030" is a mistake).
MAX_DELAY = timedelta(days=365)

#: A wake-up more than this late (downtime, long sibling tools) is announced
#: as such in the wake-up message. Normal tick jitter stays under ~1s, so
#: anything past this window means the wake-up was genuinely delayed.
LATE_GRACE = 60.0

_REL_RE = re.compile(r"^\+(\d+)([smhd])$")
_ABS_FORMAT = "%Y-%m-%d %H:%M"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _err(msg: str) -> str:
    """Error text for the model, always carrying the current local time so
    it can correct an absolute target (it often does not know the time)."""
    return f"Error: {msg} (current local time: {_now_str()})"


def parse_sleep_until(until: str) -> tuple[float, str | None]:
    """Parse the ``sleep_until`` argument into (wake_at, error).

    Exactly two formats are accepted (local time): a relative duration
    ``+[n][s|m|h|d]`` and an absolute ``YYYY-MM-DD HH:MM``. Anything else,
    or a target outside [MIN_DELAY, MAX_DELAY], yields an error string.
    """
    text = (until or "").strip()
    now = datetime.now()
    if not text:
        return 0.0, _err("missing 'until' (a '+30m' duration or a 'YYYY-MM-DD HH:MM' local time)")

    m = _REL_RE.match(text)
    if m:
        n = int(m.group(1))
        seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        wake = now + timedelta(seconds=n * seconds)
    else:
        try:
            wake = datetime.strptime(text, _ABS_FORMAT)
        except ValueError:
            wake = None
    if wake is None:
        return 0.0, _err(
            f"unrecognized time '{text}'. Use a relative duration like "
            f"'+30m' / '+2h' / '+1d' / '+90s', or an absolute local time "
            f"'YYYY-MM-DD HH:MM'"
        )

    delay = (wake - now).total_seconds()
    if delay < MIN_DELAY:
        return 0.0, _err(
            f"the target time must be at least {MIN_DELAY:.0f}s in the future "
            f"(got {delay:.0f}s); to wait briefly just continue working"
        )
    if delay > MAX_DELAY.total_seconds():
        return 0.0, _err(
            f"the target time is too far away ({delay / 86400:.0f} days; "
            f"max {MAX_DELAY.days})"
        )
    return wake.timestamp(), None


def format_delay(seconds: float) -> str:
    """Human-friendly duration for the sleep placeholder ('8h', '45m')."""
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"


def wake_message(wake_at: float | None = None) -> str:
    """The synthetic user message that wakes a suspended session.

    Carries the current local time. A wake-up that is meaningfully late
    (server downtime or long sibling tools delayed it past the grace
    window) also names the original target and the lateness, so the model
    can immediately judge whether the suspended task is still relevant.
    """
    now = datetime.now()
    text = f"{WAKE_PREFIX}（{now.strftime('%Y-%m-%d %H:%M:%S')}）"
    if wake_at is not None:
        late = now.timestamp() - wake_at
        if late > LATE_GRACE:
            target = datetime.fromtimestamp(wake_at).strftime("%Y-%m-%d %H:%M")
            text = (
                f"{WAKE_PREFIX}（{now.strftime('%Y-%m-%d %H:%M:%S')}，"
                f"原定 {target}，迟到了 {format_delay(late)}）"
            )
    return text


def sleep_placeholder(wake_at: float) -> str:
    """Result text recorded for the sleep_until tool call."""
    until = datetime.fromtimestamp(wake_at).strftime("%Y-%m-%d %H:%M")
    delay = format_delay(wake_at - time.time())
    return (
        f"Sleeping until {until} ({delay} from now). This run is suspended -- "
        f"no tokens are spent while you sleep. The session wakes automatically "
        f"at that time with a wake-up message containing the current time, and "
        f"you resume this conversation where you left off. If the user sends a "
        f"message in the meantime, you resume early."
    )


class TimerService:
    """Holds every session's suspension; ticks every second and wakes due ones.

    Owned by the server. Suspensions persist as the ``sleep_until`` field of
    each session's ``session.json`` and are reloaded at startup; the agent
    factory is injected so a due wake-up can restore the agent from disk.
    """

    def __init__(self, sessions_dir: Path, get_agent: Callable[[str], Any]):
        self._sessions_dir = sessions_dir
        self._get_agent = get_agent
        #: session name -> wake-at epoch seconds.
        self._pending: dict[str, float] = {}
        self._tick_task: asyncio.Task | None = None

    # ── persistence ────────────────────────────────────────────────────────

    def _session_json(self, name: str) -> Path:
        return self._sessions_dir / name / "session.json"

    def _load(self, name: str) -> float | None:
        path = self._session_json(name)
        try:
            if not path.is_file():
                return None
            info = json.loads(path.read_text())
            wake_at = info.get(SLEEP_FIELD)
            return float(wake_at) if isinstance(wake_at, (int, float)) else None
        except Exception as e:
            _log.warning("session.json load failed for %s: %r", name, e)
            return None

    def load_all(self) -> None:
        """Reload every session's suspension from disk (startup / rescan).

        Wake times that fell while the service was down stay armed -- the
        tick fires them immediately after startup as late wake-ups (the
        wake-up message carries the real current time).
        """
        self._pending.clear()
        if not self._sessions_dir.exists():
            return
        for sd in self._sessions_dir.iterdir():
            if not (sd.is_dir() and (sd / "session.json").is_file()):
                continue
            wake_at = self._load(sd.name)
            if wake_at is not None:
                self._pending[sd.name] = wake_at

    # ── tool-facing API (called from the agent, synchronous) ───────────────

    def register(self, name: str, wake_at: float) -> None:
        """(Re-)arm a session's suspension; persists to ``session.json``.

        Read-modify-write keeps every other field intact (mode switches and
        model switches rewrite the same file while a session is suspended).
        A second ``sleep_until`` call overwrites the first target.
        """
        self._pending[name] = wake_at
        path = self._session_json(name)
        try:
            info = json.loads(path.read_text()) if path.is_file() else {}
            info[SLEEP_FIELD] = wake_at
            path.write_text(json.dumps(info, indent=2, ensure_ascii=False))
        except Exception as e:
            _log.warning("sleep_until persist failed for %s: %r", name, e)

    def cancel(self, name: str) -> None:
        """Drop a suspension without waking (no-op when not sleeping)."""
        if self._pending.pop(name, None) is not None:
            self._clear_field(name)

    def _clear_field(self, name: str) -> None:
        path = self._session_json(name)
        try:
            if not path.is_file():
                return
            info = json.loads(path.read_text())
            if SLEEP_FIELD in info:
                info.pop(SLEEP_FIELD, None)
                path.write_text(json.dumps(info, indent=2, ensure_ascii=False))
        except Exception as e:
            _log.warning("sleep_until clear failed for %s: %r", name, e)

    def remove_session(self, name: str) -> None:
        """Forget a deleted session's suspension (its dir is removed by the
        caller, so no file cleanup is needed here)."""
        self._pending.pop(name, None)

    # ── session-facing API (called from the server) ────────────────────────

    def is_sleeping(self, name: str) -> bool:
        return name in self._pending

    def sleep_until(self, name: str) -> float | None:
        return self._pending.get(name)

    async def wake(self, name: str) -> bool:
        """Wake a suspended session now (REST endpoint). False when it is
        not suspended (or its agent is mid-run -- send a message instead)."""
        return await self._do_wake(name)

    # ── tick loop ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._tick_task is None:
            self._tick_task = asyncio.create_task(self._tick())

    async def shutdown(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None

    async def _tick(self) -> None:
        """Every second, wake the sessions whose target time has arrived."""
        while True:
            try:
                now = time.time()
                for name, wake_at in list(self._pending.items()):
                    if now >= wake_at:
                        await self._do_wake(name)
            except Exception:
                _log.exception("timer tick error")
            await asyncio.sleep(1)

    async def _do_wake(self, name: str) -> bool:
        """Clear the suspension and start the wake-up run.

        Returns False only when the session is not suspended. When the
        agent is mid-run (long sibling tools from the sleeping turn) the
        suspension is re-armed -- file field included, so a crash in between
        keeps it recoverable -- and the tick retries once the run ends;
        the wake-up itself is never dropped.
        """
        wake_at = self._pending.pop(name, None)
        if wake_at is None:
            return False
        try:
            agent = self._get_agent(name)
        except Exception:
            # The session was deleted behind our back (the endpoint's 404).
            return True
        if agent.is_running():
            self._pending[name] = wake_at
            return True
        self._clear_field(name)
        # The suspension is over regardless of how the wake-up run ends;
        # tell the frontend now so the blue dot clears even when the agent
        # was restored from disk (no in-memory flag to key the run
        # prologue's sleep_end on).
        await agent._send_stream_event({"type": "sleep_end", "session": name})
        # start()'s synchronous prefix (append user message + set_running)
        # executes before its first await, so no /message request can
        # interleave between the is_running check above and the running
        # flag flipping -- the same race-free shape /message relies on.
        await agent.start(wake_message(wake_at))
        return True

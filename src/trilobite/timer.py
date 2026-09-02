"""Session timers (sleep_until).

The ``sleep_until`` virtual tool suspends a session until a target time
(see ``doc/product/timer.md``). A suspension is a single ``sleep_until``
field in the session's ``session.json`` (epoch seconds) plus an in-memory
pending table; while suspended the session spends no tokens and its broker
sits idle (the sidebar shows a blue dot).

Design notes:

- From the model's view ``sleep_until`` is an ordinary tool whose result is
  simply slow: no result is produced when the call executes. When the
  session wakes, the deferred result (saying on schedule / early / late,
  with the real current time) is inserted into the sleeping batch's
  ToolResults entry and a normal run starts -- no synthetic user message is
  involved, and the model sees the whole sleeping batch's results together.
- A user message during the suspension interrupts it: the deferred result
  (marked as interrupted) is delivered right before the user's message, the
  suspension is gone, and whether to sleep again is the model's own next
  decision.
- The stop button cancels the suspension without a run: the deferred result
  (marked aborted) sits in history like any stopped tool call's, and the
  session idles waiting for user input.
- Wake times that fell during downtime stay armed and fire right after
  startup; the deferred result carries the real current time, so a late
  wake still tells the model the truth.
- A session whose agent is still running when its wake time arrives (the
  sleeping turn's batch is still finishing) keeps its suspension armed; the
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

#: Why a suspension ended -- selects the deferred result's wording (see
#: ``sleep_result_text``).
SLEEP_WOKE = "wake"                 # timer tick or POST /wake
SLEEP_INTERRUPTED = "user_message"  # a user message interrupted the suspension
SLEEP_CANCELLED = "cancelled"       # a user message arrived before it took effect
SLEEP_ABORTED = "aborted"           # stop-button interrupt of the suspension
SLEEP_SUPERSEDED = "superseded"     # a later sleep_until in the same batch won

#: A suspension must be at least this far in the future (shorter waits are
#: rejected as pointless -- the model can just continue working).
MIN_DELAY = 5.0

#: ...and at most this far (a year; "wake me in 2030" is a mistake).
MAX_DELAY = timedelta(days=365)

#: A wake-up more than this late (downtime, long sibling tools) is announced
#: as such in the wake-up message. Normal tick jitter stays under ~1s, so
#: anything past this window means the wake-up was genuinely delayed.
LATE_GRACE = 60.0

#: Relative duration, segments in strictly descending unit order (d>h>m>s),
#: each unit at most once: ``+30m``, ``+4h50m``, ``+1d2h30m``.
_REL_RE = re.compile(r"^\+(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
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
    ``+<n><unit>[<n><unit>...]`` (one or more ``d``/``h``/``m``/``s``
    segments in descending unit order, each at most once, e.g. ``+30m``
    or ``+4h50m``) and an absolute ``YYYY-MM-DD HH:MM``. Anything else,
    or a target outside [MIN_DELAY, MAX_DELAY], yields an error string.
    """
    text = (until or "").strip()
    now = datetime.now()
    if not text:
        return 0.0, _err("missing 'until' (a '+30m' duration or a 'YYYY-MM-DD HH:MM' local time)")

    m = _REL_RE.match(text)
    if m and any(m.groups()):
        d, h, mi, s = (int(g or 0) for g in m.groups())
        wake = now + timedelta(seconds=d * 86400 + h * 3600 + mi * 60 + s)
    else:
        try:
            wake = datetime.strptime(text, _ABS_FORMAT)
        except ValueError:
            wake = None
    if wake is None:
        return 0.0, _err(
            f"unrecognized time '{text}'. Use a relative duration like "
            f"'+30m' / '+2h' / '+4h50m' / '+1d2h30m' / '+90s' (largest "
            f"unit first), or an absolute local time 'YYYY-MM-DD HH:MM'"
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
    """Human-friendly duration ('8h', '45m') for lateness notes."""
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"


def sleep_result_text(wake_at: float | None, reason: str) -> str:
    """The deferred sleep_until tool result, built when the session wakes.

    ``wake_at`` is the armed target (epoch seconds). For ``SLEEP_WOKE`` the
    clock decides the wording: on time, early (manual /wake), or late --
    past ``LATE_GRACE`` either way is called out explicitly so the model can
    judge whether the suspended task is still relevant. Every wording carries
    the real current time.
    """
    now = datetime.now()
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    if reason == SLEEP_CANCELLED:
        return (
            "sleep_until cancelled before the suspension started: a user "
            f"message arrived at {now_s}. Respond to the user now; sleep "
            "again afterwards if the wait is still needed."
        )
    if reason == SLEEP_SUPERSEDED:
        return (
            "Superseded by a later sleep_until call in the same turn; "
            "this call never took effect."
        )
    target = (
        datetime.fromtimestamp(wake_at).strftime("%Y-%m-%d %H:%M")
        if wake_at is not None
        else "unknown"
    )
    if reason == SLEEP_INTERRUPTED:
        return (
            f"Interrupted by a user message at {now_s} (target was {target}). "
            "The user's message follows this result -- respond to it now and "
            "decide afterwards whether to sleep again."
        )
    if reason == SLEEP_ABORTED:
        return (
            f"sleep_until interrupted by the user at {now_s} (target was "
            f"{target}). The suspension is over -- the session is idle; wait "
            "for the user's next message."
        )
    # SLEEP_WOKE: on time, early (manual wake), or late (downtime).
    late = now.timestamp() - wake_at if wake_at is not None else 0.0
    if late > LATE_GRACE:
        return (
            f"Woke at {now_s} -- {format_delay(late)} late (the server was "
            f"down or busy); target was {target}. Judge whether the "
            "suspended task is still relevant."
        )
    if late < -LATE_GRACE:
        return f"Woken early by the user at {now_s}; target was {target}."
    return f"Woke on schedule at {now_s} (target {target})."


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
        not suspended (or its agent is mid-run -- the wake-up is armed for
        when the run ends)."""
        return await self._do_wake(name)

    async def abort(self, name: str) -> bool:
        """Stop-button interrupt of a suspension: deliver the deferred
        results as aborted and stop -- no wake-up run starts. Like any
        stopped tool call, the interrupted result sits in history (the model
        sees it on the next run) and the session idles, waiting for user
        input. False when the session is not suspended."""
        wake_at = self._pending.pop(name, None)
        if wake_at is None:
            return False
        self._clear_field(name)
        try:
            agent = self._get_agent(name)
        except Exception:
            # The session was deleted behind our back (the endpoint's 404).
            return True
        await agent.interrupt_sleep(wake_at)
        return True

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

    async def _do_wake(self, name: str, reason: str = SLEEP_WOKE) -> bool:
        """Clear the suspension and start the wake-up run.

        Returns False only when the session is not suspended. When the
        agent is mid-run (the sleeping turn's batch is still finishing) the
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
        # resume_from_sleep delivers the deferred sleep results (on time,
        # late, early, or aborted -- the reason decides), sends sleep_end so
        # the blue dot clears even when the agent was restored from disk,
        # and starts the wake-up run. Its synchronous prefix executes before
        # its first await, so no /message request can interleave between the
        # is_running check above and the running flag flipping -- the same
        # race-free shape /message relies on.
        await agent.resume_from_sleep(wake_at, reason)
        return True

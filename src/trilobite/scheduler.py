"""Scheduled subagents (cron).

A schedule ties a 5-field cron expression to a prompt. When the cron fires,
the :class:`CronService` spawns a fresh scheduled agent -- an unattended,
fire-and-forget run whose results never return to the main agent. The main
agent manages schedules through the ``cron_create`` / ``cron_list`` /
``cron_delete`` virtual tools (add / list / delete -- there is no edit
tool, so schedules are immutable once created; adjust by delete + create).

Design notes (see ``doc/product/scheduled_subagent.md``):

- Each schedule owns one session (``kind: "scheduled"`` in session.json).
  Every fire REUSES that session: the agent instance is recreated (or
  reused if already in the registry) and a :class:`CompactMarker` is
  appended before the fire's messages, so the API context is fresh (the
  marker restarts ``get_api_messages``) while the persisted history
  accumulates across fires. The first user message of each run is the
  synthetic ``⏰ 定时触发（<time>）`` line, which doubles as the
  frontend's run boundary.
- Scheduled agents are unattended: out-of-workspace access is denied
  directly (no interactive approval banner -- nobody would answer it).
- Fires are skipped while the previous fire of the same schedule is still
  running. Matching is minute-granular (``croniter.match`` ignores seconds):
  each tick tests the current time against the cron expression, and the
  minute-level ``last_fire_at`` check ensures one fire per matching minute,
  so a missed minute is never re-announced afterwards.
- After a server restart only future matches count; missed fires during
  downtime are not caught up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from croniter import croniter

from src.trilobite.agent import Agent
from src.trilobite.messages import SystemMessage, UserMessage

_log = logging.getLogger(__name__)

MAX_SCHEDULES_PER_SESSION = 20
MAX_PROMPT_BYTES = 8 * 1024
#: A one-shot (non-recurring) schedule must match within this window, else
#: the create call is rejected as unreachable.
FIVE_YEARS = timedelta(days=5 * 365)
#: Schedules.json filename inside a main session directory.
SCHEDULES_FILE = "schedules.json"

FIRE_PREFIX = "⏰ 定时触发"


class Schedule:
    """One cron schedule, persisted inside the owning main session."""

    def __init__(
        self,
        id: str,
        session_id: str,
        cron: str,
        prompt: str,
        recurring: bool,
        description: str,
        created_at: float,
        run_count: int = 0,
        last_state: str | None = None,
        last_fire_at: float | None = None,
        completed: bool = False,
        deleted: bool = False,
        name: str = "",
    ):
        self.id = id
        #: The scheduled agent's session id (one session per schedule).
        self.session_id = session_id
        self.cron = cron
        self.prompt = prompt
        self.recurring = recurring
        #: Short model-given name; falls back to a prompt preview when absent.
        self.name = name
        #: Sidebar label: the name when given, else a prompt preview.
        self.description = description
        self.created_at = created_at
        self.run_count = run_count
        self.last_state = last_state
        self.last_fire_at = last_fire_at
        #: One-shot schedules stay in schedules.json after their single fire,
        #: marked completed so the owning session's info endpoint can still
        #: tell "one-shot finished" apart from "schedule deleted". Completed
        #: entries never match or fire again and are hidden from cron_list.
        self.completed = completed
        #: cron_delete marks the entry deleted instead of removing it, so the
        #: session info endpoint keeps reporting the schedule's final state
        #: (the sidebar dot shows the last run's outcome, not "pending").
        self.deleted = deleted

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "cron": self.cron,
            "prompt": self.prompt,
            "recurring": self.recurring,
            "description": self.description,
            "created_at": self.created_at,
            "run_count": self.run_count,
            "last_state": self.last_state,
            "last_fire_at": self.last_fire_at,
            "completed": self.completed,
            "deleted": self.deleted,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Schedule":
        return cls(
            id=d["id"],
            session_id=d.get("session_id", d["id"]),  # legacy safety
            cron=d["cron"],
            prompt=d["prompt"],
            recurring=bool(d.get("recurring", True)),
            description=d.get("description", d["prompt"][:40]),
            created_at=d.get("created_at", 0.0),
            run_count=d.get("run_count", 0),
            last_state=d.get("last_state"),
            last_fire_at=d.get("last_fire_at"),
            completed=bool(d.get("completed", False)),
            deleted=bool(d.get("deleted", False)),
            name=d.get("name", ""),
        )

    def next_fire_at(self, after: datetime | None = None) -> datetime | None:
        """Next fire time, or None when the cron never matches within 5 years."""
        base = after or datetime.fromtimestamp(self.last_fire_at or self.created_at)
        try:
            nxt = croniter(self.cron, base).get_next(datetime)
        except (ValueError, KeyError):
            return None
        if nxt - datetime.now() > FIVE_YEARS:
            return None
        return nxt


class CronService:
    """Holds every schedule of every session; ticks every second and fires.

    Owned by the server. Schedules are persisted per owning session in
    ``<session_dir>/schedules.json`` and reloaded at startup; only future
    matches count after a restart.
    """

    def __init__(self, sessions_dir: Path, config: dict, agents: dict[str, Agent]):
        self._sessions_dir = sessions_dir
        self._config = config
        self._agents = agents
        self._schedules: dict[str, list[Schedule]] = {}
        #: schedule_id -> scheduled session name currently firing.
        self._running: dict[str, str] = {}
        #: schedule_id -> minute when a skip was last announced (dedup: a
        #: missed fire is reported at most once per matching minute).
        self._missed_minutes: dict[str, int] = {}
        self._tick_task: asyncio.Task | None = None

    # ── persistence ────────────────────────────────────────────────────────

    def _path_for(self, session_name: str) -> Path:
        return self._sessions_dir / session_name / SCHEDULES_FILE

    def _load(self, session_name: str) -> list[Schedule]:
        path = self._path_for(session_name)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text())
            return [Schedule.from_dict(d) for d in data.get("schedules", [])]
        except Exception as e:
            _log.warning("schedules load failed for %s: %r", path, e)
            return []

    def _persist(self, session_name: str, schedules: list[Schedule]) -> None:
        path = self._path_for(session_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "schedules": [s.to_dict() for s in schedules]}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    def load_all(self) -> None:
        """Reload every session's schedules from disk (startup / rescan).

        Fires that fell during downtime are NOT caught up: each schedule's
        ``last_fire_at`` is aligned to the most recent *past* cron match, so
        the tick resumes from the next future match. One-shot schedules that
        came due during downtime roll forward to their next match instead of
        firing immediately.
        """
        self._schedules.clear()
        if not self._sessions_dir.exists():
            return
        now = datetime.now()
        for sd in self._sessions_dir.iterdir():
            if sd.is_dir() and (sd / SCHEDULES_FILE).is_file():
                schedules = self._load(sd.name)
                for s in schedules:
                    self._align_past(s, now)
                self._schedules[sd.name] = schedules

    def _align_past(self, sched: Schedule, now: datetime) -> None:
        """Skip fires that fell while the service was down.

        Anchors ``last_fire_at`` to the most recent cron match strictly
        before ``now`` (croniter's get_prev is strict, so the anchor is
        always in the past). After a restart this doubles as a dedup anchor:
        if the current minute already matches, ``_already_handled`` sees the
        same minute and skips the fire, so downtime fires are never replayed.
        """
        if sched.completed or sched.deleted:
            return
        try:
            prev = croniter(sched.cron, now).get_prev(datetime)
        except (ValueError, KeyError):
            return
        if prev is not None:
            sched.last_fire_at = prev.timestamp()

    # ── tool-facing API (called from the main agent, synchronous) ──────────

    def handle(
        self,
        session_name: str,
        tool_name: str,
        args: dict[str, Any],
        working_dir: Path,
        additional_dirs: list[Path],
    ) -> str:
        """Dispatch one cron tool call; returns the text result for the LLM."""
        if tool_name == "cron_create":
            return self._create(session_name, args, working_dir, additional_dirs)
        if tool_name == "cron_list":
            return self._list(session_name)
        if tool_name == "cron_delete":
            return self._delete(session_name, args)
        return f"Error: unknown cron tool '{tool_name}'."

    def _create(
        self,
        session_name: str,
        args: dict[str, Any],
        working_dir: Path,
        additional_dirs: list[Path],
    ) -> str:
        cron = str(args.get("cron") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        recurring = bool(args.get("recurring", True))
        name = str(args.get("name") or "").strip()
        if not cron or not prompt:
            return "Error: cron_create requires 'name', 'cron' (5-field expression) and 'prompt'."
        if not croniter.is_valid(cron):
            return f"Error: invalid cron expression '{cron}'. Expected 5 fields: minute hour day-of-month month day-of-week (local time)."
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            return f"Error: prompt too large (max {MAX_PROMPT_BYTES} bytes)."

        schedules = self._schedules.get(session_name)
        if schedules is None:
            schedules = self._load(session_name)
            self._schedules[session_name] = schedules
        if sum(1 for s in schedules if not s.completed and not s.deleted) >= MAX_SCHEDULES_PER_SESSION:
            return f"Error: schedule limit reached ({MAX_SCHEDULES_PER_SESSION} per session). Delete one first."

        now = datetime.now()
        probe = Schedule(
            id="", session_id="", cron=cron, prompt=prompt, recurring=recurring,
            description="", created_at=now.timestamp(),
        )
        nxt = probe.next_fire_at(after=now)
        if nxt is None:
            if recurring:
                return f"Error: cron '{cron}' does not match within the next 5 years."
            return f"Error: cron '{cron}' never matches within 5 years; a one-shot schedule needs a reachable time."

        sched_id = uuid.uuid4().hex
        sched_session = uuid.uuid4().hex
        #: Sidebar label: the model-given name, else a whitespace-collapsed
        #: 40-char prompt preview.
        description = name or " ".join(prompt.split())[:40]

        # The schedule's own session is created up front so it shows up in the
        # sidebar immediately and accumulates runs over the schedule's life.
        session_dir = self._sessions_dir / sched_session
        session_dir.mkdir(parents=True, exist_ok=True)
        info = {
            "name": sched_session,
            "working_dir": str(working_dir),
            "parent_session": session_name,
            "kind": "scheduled",
            "schedule_id": sched_id,
            "description": description,
            "additional_dirs": [str(d) for d in additional_dirs],
            "created_at": time.time(),
        }
        (session_dir / "session.json").write_text(json.dumps(info, indent=2))

        sched = Schedule(
            id=sched_id, session_id=sched_session, cron=cron, prompt=prompt,
            recurring=recurring, description=description, created_at=now.timestamp(),
            name=name,
        )
        schedules.append(sched)
        self._persist(session_name, schedules)
        return (
            f"Schedule created (id={sched_id}, cron='{cron}', recurring={recurring}, "
            f"next fire at {nxt.strftime('%Y-%m-%d %H:%M')}). "
            f"It runs unattended as its own scheduled agent; results are NOT returned to you. "
            f"View runs in the sidebar under '{description}'. Adjust by deleting and recreating."
        )

    def _list(self, session_name: str) -> str:
        schedules = self._schedules.get(session_name)
        if schedules is None:
            schedules = self._load(session_name)
            self._schedules[session_name] = schedules
        if not schedules:
            return "No schedules in this session. Use cron_create to add one."
        lines = []
        for s in schedules:
            if s.deleted:
                # Deleted schedules leave the listing: cron_delete is an
                # explicit action, so the entry vanishing is the feedback.
                continue
            if s.completed:
                lines.append(
                    f"- id={s.id} cron='{s.cron}' recurring={s.recurring} [completed] "
                    f"runs={s.run_count} last={s.last_state or 'never'} desc='{s.description}'"
                )
                continue
            nxt = s.next_fire_at()
            nxt_s = nxt.strftime("%Y-%m-%d %H:%M") if nxt else "none within 5y"
            lines.append(
                f"- id={s.id} cron='{s.cron}' recurring={s.recurring} "
                f"runs={s.run_count} last={s.last_state or 'never'} "
                f"next={nxt_s} desc='{s.description}'"
            )
        if not lines:
            return "No schedules in this session. Use cron_create to add one."
        return "Schedules:\n" + "\n".join(lines)

    def _delete(self, session_name: str, args: dict[str, Any]) -> str:
        sched_id = str(args.get("id") or "").strip()
        schedules = self._schedules.get(session_name)
        if schedules is None:
            schedules = self._load(session_name)
            self._schedules[session_name] = schedules
        for s in schedules:
            if s.id == sched_id:
                # Mark deleted (keep the entry) so the owning session's info
                # endpoint can still report the schedule's final state and
                # one-shot-ness; the sidebar keeps showing the last run's
                # outcome instead of a misleading "pending" dot.
                s.deleted = True
                self._persist(session_name, schedules)
                return (
                    f"Schedule {sched_id} deleted; no further fires. "
                    f"Its session '{s.description}' stays in the sidebar for reviewing past runs."
                )
        return f"Error: schedule '{sched_id}' not found."

    def remove_session(self, session_name: str) -> None:
        """Drop every schedule of a deleted main session."""
        self._schedules.pop(session_name, None)
        path = self._path_for(session_name)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    def delete_schedule(self, session_name: str, sched_id: str) -> str:
        """UI-facing cancel: same semantics as the cron_delete tool."""
        return self._delete(session_name, {"id": sched_id})

    def remove_schedule_by_session(self, sched_session_id: str) -> None:
        """Drop the schedule owning a deleted scheduled session (cascade)."""
        for session_name, schedules in list(self._schedules.items()):
            for s in list(schedules):
                if s.session_id == sched_session_id:
                    schedules.remove(s)
                    self._persist(session_name, schedules)
                    self._running.pop(s.id, None)
                    self._missed_minutes.pop(s.id, None)

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
        """Every second, walk every schedule and fire those whose cron
        expression matches the current time.

        Matching is minute-granular (``croniter.match`` ignores the seconds
        of a 5-field expression), so any tick within the matching minute
        qualifies -- a skipped second never loses a fire. Duplicate fires
        within the same minute are blocked by the minute-level
        ``last_fire_at`` check (``_already_handled``); one-shot schedules
        are marked ``completed`` on their first match, so they cannot refire.
        """
        while True:
            try:
                now = datetime.now()
                minute = int(now.timestamp()) // 60
                for session_name, schedules in list(self._schedules.items()):
                    for sched in list(schedules):
                        if sched.completed or sched.deleted:
                            continue
                        if sched.id in self._running:
                            # The previous fire is still going: skip this
                            # minute's match, announcing it once per minute.
                            if self._matches(sched, now) and self._missed_minutes.get(sched.id) != minute:
                                self._missed_minutes[sched.id] = minute
                                asyncio.create_task(self._send_event(session_name, {
                                    "type": "cron_missed",
                                    "schedule_id": sched.id,
                                    "session": sched.session_id,
                                }))
                            continue
                        if not self._matches(sched, now):
                            continue
                        if self._already_handled(sched, now):
                            continue
                        if sched.recurring:
                            # Mark this cron minute as handled before firing
                            # so later ticks in the same minute skip it.
                            sched.last_fire_at = now.timestamp()
                        else:
                            # One-shot: fire and mark completed (kept in the
                            # file so the session info endpoint can still
                            # report its final state and one-shot-ness).
                            sched.completed = True
                        self._persist(session_name, schedules)
                        asyncio.create_task(self._fire(session_name, sched))
            except Exception:
                _log.exception("cron tick error")
            await asyncio.sleep(1)

    def _matches(self, sched: Schedule, now: datetime) -> bool:
        """True when ``now`` (minute-granular) matches the schedule's cron."""
        try:
            return croniter.match(sched.cron, now)
        except (ValueError, KeyError):
            return False

    def _already_handled(self, sched: Schedule, now: datetime) -> bool:
        """True when the current cron minute was already fired (or is firing).

        ``last_fire_at`` is set to the fire moment before firing; the
        ``_fire`` completion path overwrites it with the actual completion
        time, so the check compares minutes, not timestamps.
        """
        if sched.last_fire_at is None:
            return False
        return int(sched.last_fire_at) // 60 == int(now.timestamp()) // 60

    # ── firing ─────────────────────────────────────────────────────────────

    async def _send_event(self, session_name: str, event: dict) -> None:
        """Emit a cron event to the owning main session's stream (if alive)."""
        agent = self._agents.get(session_name)
        if agent is not None:
            await agent._send_stream_event(event)

    async def _fire(self, session_name: str, sched: Schedule) -> None:
        session_dir = self._sessions_dir / sched.session_id
        if not (session_dir / "session.json").is_file():
            # The scheduled session was deleted behind our back.
            self.remove_schedule_by_session(sched.session_id)
            return
        info = json.loads((session_dir / "session.json").read_text())

        agent = self._agents.get(sched.session_id)
        if agent is not None and agent.is_running():
            return  # a fire is already running (missed handling is in _tick)

        # Every fire is a fresh agent instance -- zero API context (the
        # CompactMarker appended by start_scheduled_fire drops the previous
        # runs from get_api_messages, while they stay in the persisted
        # history for viewing). It replaces the previous instance in the
        # registry so interrupt / stream endpoints always see the live one.
        agent = Agent(
            name=sched.session_id,
            working_dir=info["working_dir"],
            session_dir=session_dir,
            config=self._config,
            registry=self._agents,
            scheduled=True,
            scheduled_allow_dirs=info.get("additional_dirs", []),
            max_steps=int(self._config.get("subagent_max_steps", 100)),
        )
        agent.set_additional_dirs(info.get("additional_dirs", []))
        self._agents[sched.session_id] = agent

        self._running[sched.id] = sched.session_id
        fire_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        text = f"{FIRE_PREFIX}（{fire_time}）\n{sched.prompt}"
        await agent.start_scheduled_fire(text)
        await self._send_event(session_name, {
            "type": "cron_fire",
            "schedule_id": sched.id,
            "session": sched.session_id,
            "description": sched.description,
            "state": "running",
        })
        try:
            await agent.run()
        except asyncio.CancelledError:
            pass
        finally:
            self._running.pop(sched.id, None)
            sched.run_count += 1
            sched.last_state = agent._final_state
            sched.last_fire_at = time.time()
            self._persist(session_name, self._schedules.get(session_name, []))
            await self._send_event(session_name, {
                "type": "cron_fire_end",
                "session": sched.session_id,
                "state": agent._final_state,
            })

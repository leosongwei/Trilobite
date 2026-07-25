from __future__ import annotations

import asyncio


class StreamBroker:
    """Per-session event bus that decouples agent runs from HTTP requests.

    A connected SSE client is represented by an ``asyncio.Queue`` in
    ``_subscribers``. Every published event is fanned out to all of them, so
    multiple browsers (or a browser reopened after closing) see the same live
    stream.

    Reconnect / multi-open correctness relies on two pieces of state:

    * ``_turn_buffer`` – the events of the *current run* (from the first
      ``turn`` up to now). When a client attaches mid-run these are replayed
      into its queue so it catches up on the in-progress output.
    * ``_persisted_len`` – how many history messages are already "committed",
      i.e. safe to send in the ``init`` snapshot. The replayed buffer mirrors
      history *beyond* ``_persisted_len``, so the snapshot
      (``history[:persisted_len]``) and the replayed buffer never overlap and
      nothing is lost on reconnect.

    The agent owns the history; the broker only tracks ``_persisted_len`` and
    the buffer, and is told the current history length on every publish so it
    can advance ``_persisted_len`` when a run finishes.
    """

    def __init__(self, persisted_len: int = 0) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._turn_buffer: list[dict] = []
        self._is_running: bool = False
        self._persisted_len: int = persisted_len
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._is_running

    def set_running(self, running: bool) -> None:
        self._is_running = running

    async def publish(self, event: dict, history_len: int) -> None:
        """Broadcast ``event`` to every subscriber and manage the buffer.

        ``turn`` starts a run (buffer accumulates). ``done``/``cancelled``/
        ``error`` end it: the run's messages are now in history, so
        ``_persisted_len`` advances and the buffer is dropped to avoid
        duplicating them on a later reconnect.
        """
        t = event.get("type")
        async with self._lock:
            if t == "turn":
                self._turn_buffer.append(event)
                self._is_running = True
            elif t in ("done", "cancelled", "error", "interrupted"):
                self._is_running = False
            else:
                self._turn_buffer.append(event)
                # When a tool finishes, drop its streamed output lines from
                # the replay buffer: the final (truncated) result is now in
                # the tool_result event, and keeping thousands of
                # tool_output lines would make reconnect replay painfully
                # slow.
                if t == "tool_result":
                    tcid = event.get("tool_call_id")
                    if tcid is not None:
                        self._turn_buffer = [
                            e for e in self._turn_buffer
                            if not (e.get("type") == "tool_output"
                                    and e.get("tool_call_id") == tcid)
                        ]

            for q in list(self._subscribers):
                q.put_nowait(event)

            if t in ("done", "cancelled", "error", "interrupted"):
                self._persisted_len = history_len
                self._turn_buffer = []

    async def commit(self, history_len: int) -> None:
        """Advance ``_persisted_len`` and clear the buffer.

        Called after compaction rewrites history: the messages the buffered
        events were mirroring are now part of history, so the buffer must be
        dropped or a reconnect would render them twice.
        """
        async with self._lock:
            self._persisted_len = history_len
            self._turn_buffer = []

    async def attach(
        self,
        history_raw: list[dict],
        token_count: int,
        max_context_tokens: int,
        plan_mode: bool,
        additional_dirs: list[str],
    ) -> tuple[asyncio.Queue, dict]:
        """Subscribe a new client.

        Replay the current run's buffered events into the client's queue, then
        build the ``init`` snapshot from history up to ``_persisted_len``.
        Everything happens under the lock so the snapshot and the replay see a
        consistent point-in-time view (no event lost, none duplicated).
        """
        async with self._lock:
            q: asyncio.Queue = asyncio.Queue()
            for ev in self._turn_buffer:
                q.put_nowait(ev)
            self._subscribers.add(q)
            snapshot = {
                "history": list(history_raw[: self._persisted_len]),
                "is_running": self._is_running,
                "token_count": token_count,
                "max_context_tokens": max_context_tokens,
                "plan_mode": plan_mode,
                "additional_dirs": additional_dirs,
            }
            return q, snapshot

    def detach(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

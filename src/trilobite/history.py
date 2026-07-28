from __future__ import annotations

import json
import logging
from pathlib import Path

from src.trilobite.messages import (
    AssistantMessage,
    CompactMarker,
    Message,
    UserMessage,
    combine_new_messages,
    from_v1,
    message_from_storage,
)

_log = logging.getLogger(__name__)


class History:
    """Typed conversation history with v2 persistence and v1 compatibility.

    Messages are stored as typed objects (:class:`SystemMessage`,
    :class:`UserMessage`, :class:`CompactMarker`, :class:`AssistantMessage`).
    Persisted as v2 JSON (``{"version": 2, "messages": [...]}``). Legacy v1
    files (flat dict arrays) are merged into objects on load and rewritten as
    v2 on the first save (lazy upgrade), so a session touched by the new code
    is migrated automatically.

    ``get_api_messages()`` projects to OpenAI-compatible dicts: it starts from
    the last :class:`CompactMarker` (dropping everything before it from the API
    context while keeping it in persisted history) and merges consecutive user
    messages (text joined by ``\\n\\n``) to avoid API issues with repeated
    same-role messages.
    """

    def __init__(self, path: Path):
        self._path = path
        self._messages: list[Message] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except Exception as e:
            _log.warning("history load failed for %s: %r", self._path, e)
            self._messages = []
            return
        if isinstance(data, list):
            # v1: flat dict array -> merge into typed objects.
            self._messages = from_v1(data)
        elif isinstance(data, dict) and data.get("version") == 2:
            self._messages = [message_from_storage(m) for m in data.get("messages", [])]
        else:
            _log.warning("history has unknown format for %s", self._path)
            self._messages = []

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "messages": [m.to_storage_dict() for m in self._messages],
        }
        self._path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    def append(self, message: Message, persist: bool = True) -> None:
        self._messages.append(message)
        if persist:
            self.save()

    def extend(self, messages: list[Message], persist: bool = True) -> None:
        self._messages.extend(messages)
        if persist:
            self.save()

    def insert(self, index: int, message: Message, persist: bool = True) -> None:
        self._messages.insert(index, message)
        if persist:
            self.save()

    def pop(self) -> Message | None:
        """Remove and return the last message (persisting the change)."""
        if not self._messages:
            return None
        m = self._messages.pop()
        self.save()
        return m

    def truncate(self, index: int) -> None:
        """Drop every message from ``index`` onward (inclusive)."""
        self._messages = self._messages[:index]
        self.save()

    def get_api_messages(self) -> list[dict]:
        """Return messages for the API, starting just past the last compact marker.

        A :class:`CompactMarker` acts as a fresh start: every message up to and
        including the last marker is dropped from the API context (a fresh
        :class:`SystemMessage` right after the marker becomes the new system
        prompt). This separates the *frontend* history (which keeps everything,
        persisted in the JSON file) from the *API* history (which only sees
        post-compaction messages).

        Consecutive user messages are combined with :func:`combine_new_messages`
        (``<multi_message/>`` separators) so the API never sees repeated
        same-role messages and the model can tell distinct user inputs apart.
        """
        start = 0
        for i, msg in enumerate(self._messages):
            if isinstance(msg, CompactMarker):
                start = i + 1  # start just past the marker

        # Drop assistant turns that have no content and no tool_calls (e.g. a
        # turn cancelled mid-stream, which leaves only thinking): their empty
        # content string is rejected by some APIs (e.g. Volcengine) and they
        # carry no model-visible output. The thinking is still kept in
        # persisted history for the frontend, and dropping them here lets the
        # surrounding user messages combine normally.
        msgs = [
            m for m in self._messages[start:]
            if not (isinstance(m, AssistantMessage) and not m.content and not m.tool_calls)
        ]
        result: list[dict] = []
        i = 0
        while i < len(msgs):
            msg = msgs[i]
            if isinstance(msg, UserMessage):
                # Collect a run of consecutive user messages and combine them.
                group: list[UserMessage] = [msg]
                i += 1
                while i < len(msgs) and isinstance(msgs[i], UserMessage):
                    group.append(msgs[i])
                    i += 1
                result.append(combine_new_messages(group))
            else:
                for d in msg.to_api_dicts():
                    result.append(d)
                i += 1
        return result

    def to_flat_dicts(self) -> list[dict]:
        """Expand all messages into the flat v1-style dict list for the frontend."""
        return [d for m in self._messages for d in m.to_frontend_dicts()]

    @property
    def raw(self) -> list[Message]:
        return self._messages

    def __len__(self) -> int:
        return len(self._messages)

    def __getitem__(self, key):
        return self._messages[key]

    def __iter__(self):
        return iter(self._messages)

    def __bool__(self) -> bool:
        return len(self._messages) > 0

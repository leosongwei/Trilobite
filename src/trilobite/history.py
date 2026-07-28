from __future__ import annotations

import json
import logging
from pathlib import Path

from src.trilobite.messages import (
    CompactMarker,
    Message,
    UserMessage,
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
        """Return messages for the API, starting from the last compact marker.

        A :class:`CompactMarker` acts as a fresh start: every message before
        the last marker is dropped from the API context. The marker's own
        content becomes the new system message. This separates the *frontend*
        history (which keeps everything, persisted in the JSON file) from the
        *API* history (which only sees post-compaction messages).

        Consecutive user messages are merged into one (text joined by
        ``\\n\\n``) to avoid API issues with repeated same-role messages.
        """
        start = 0
        for i, msg in enumerate(self._messages):
            if isinstance(msg, CompactMarker):
                start = i

        result: list[dict] = []
        for msg in self._messages[start:]:
            for d in msg.to_api_dicts():
                if d.get("role") == "user" and result and result[-1].get("role") == "user":
                    prev_content = result[-1].get("content", "") or ""
                    cur_content = d.get("content", "") or ""
                    if prev_content and cur_content:
                        result[-1] = {"role": "user", "content": f"{prev_content}\n\n{cur_content}"}
                    else:
                        result[-1] = {"role": "user", "content": prev_content or cur_content}
                else:
                    result.append(d)
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

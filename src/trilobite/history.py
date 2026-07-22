from __future__ import annotations

import json
from pathlib import Path


class History:
    """Conversation history with persistence and API-time message merging.

    Messages are stored individually (each steering message is a separate
    entry). When building messages for the API, consecutive user messages
    are merged into one (text joined by \\n\\n) to avoid API issues with
    repeated same-role messages.
    """

    def __init__(self, path: Path):
        self._path = path
        self._messages: list[dict] = []
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._messages = json.loads(self._path.read_text())
            except Exception:
                self._messages = []

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._messages, indent=2, ensure_ascii=False))

    def append(self, message: dict):
        self._messages.append(message)
        self.save()

    def extend(self, messages: list[dict]):
        self._messages.extend(messages)
        self.save()

    def insert(self, index: int, message: dict):
        self._messages.insert(index, message)
        self.save()

    def replace_all(self, messages: list[dict]):
        self._messages = list(messages)
        self.save()

    def get_api_messages(self) -> list[dict]:
        """Return a copy with consecutive user messages merged."""
        result: list[dict] = []
        for msg in self._messages:
            if msg.get("role") == "user" and result and result[-1].get("role") == "user":
                prev_content = result[-1].get("content", "") or ""
                cur_content = msg.get("content", "") or ""
                if prev_content and cur_content:
                    result[-1] = {"role": "user", "content": f"{prev_content}\n\n{cur_content}"}
                else:
                    result[-1] = {"role": "user", "content": prev_content or cur_content}
            else:
                result.append(msg.copy())
        return result

    @property
    def raw(self) -> list[dict]:
        return self._messages

    def __len__(self):
        return len(self._messages)

    def __getitem__(self, key):
        return self._messages[key]

    def __iter__(self):
        return iter(self._messages)

    def __bool__(self):
        return len(self._messages) > 0

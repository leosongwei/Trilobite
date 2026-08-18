from __future__ import annotations

import json
import logging
from pathlib import Path

from src.trilobite.messages import (
    CompactMarker,
    Message,
    ModelMessage,
    SystemMessage,
    ToolResult,
    ToolResults,
    UserMessage,
    combine_new_messages,
    from_v1,
    from_v2,
    message_from_storage,
)

_log = logging.getLogger(__name__)


class MessageList:
    """Flat conversation history: the source of truth for the message list.

    The typed message list mirrors the OpenAI completions sequence: a model
    turn is a :class:`ModelMessage` immediately followed by its
    :class:`ToolResults` entry, and user/steering messages always land after
    the results of a call batch. The ordering invariant ("all results of a
    call batch precede any later user message") is maintained by the append
    primitives (``append_user``/``append_model``/``insert_result``) -- the
    insertion position is internal bookkeeping, never the caller's concern.

    Persisted as v3 JSON (``{"version": 3, "messages": [...]}``). Legacy v1
    (bare flat dict array) and v2 (``{"version": 2}`` with nested
    ``assistant.tool_results``) files are converted on load and rewritten as
    v3 on the first save (lazy upgrade).

    ``get_api_messages()`` projects to OpenAI-compatible dicts: it starts from
    the last :class:`CompactMarker` (dropping everything before it from the
    API context while keeping it in persisted history) and merges consecutive
    user messages (text joined with ``<multi_message/>`` separators) to avoid
    API issues with repeated same-role messages.
    """

    def __init__(self, path: Path):
        self._path = path
        self._messages: list[Message] = []
        self._open_model: ModelMessage | None = None
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
            # v1: flat dict array.
            self._messages = from_v1(data)
        elif isinstance(data, dict):
            version = data.get("version")
            entries = data.get("messages", [])
            if version == 2:
                self._messages = from_v2(entries)
            elif version == 3:
                self._messages = [message_from_storage(m) for m in entries]
            else:
                _log.warning("history has unknown version %r for %s", version, self._path)
                self._messages = []
        else:
            _log.warning("history has unknown format for %s", self._path)
            self._messages = []

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 3,
            "messages": [m.to_storage_dict() for m in self._messages],
        }
        self._path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    # ── append primitives ────────────────────────────────────────────────

    def append(self, message: Message, persist: bool = True) -> None:
        """Append a message at the end (users, markers, systems, summaries).

        All of these are legal at the end of the flat list; results are the
        only messages that need a specific position and go through
        ``insert_result`` instead.
        """
        self._messages.append(message)
        if persist:
            self.save()

    def append_model(self, model: ModelMessage, persist: bool = False) -> None:
        """Start a turn: append an empty model shell (not persisted until finalized)."""
        self._messages.append(model)
        self._open_model = model
        if persist:
            self.save()

    def close_model(self) -> None:
        """Close the open model (turn finalized); results can no longer be inserted."""
        self._open_model = None

    def insert_result(self, tr: ToolResult, after: ModelMessage | None = None, persist: bool = True) -> None:
        """Insert one tool result into the ToolResults entry right after a model."""
        self.insert_results([tr], after=after, persist=persist)

    def insert_results(self, results: list[ToolResult], after: ModelMessage | None = None, persist: bool = True) -> None:
        """Insert tool results into the ToolResults entry right after a model.

        The entry is created (right after the model, before any later user
        messages) if this is the first result of the batch. ``after`` defaults
        to the open model, i.e. the turn currently executing tools. This is the
        single enforcement point for the "results before users" invariant.
        """
        model = after or self._open_model
        if model is None or model not in self._messages:
            raise ValueError("cannot insert_result without an open/in-list model")
        idx = self._messages.index(model)
        j = idx + 1
        if j < len(self._messages) and isinstance(self._messages[j], ToolResults):
            entry = self._messages[j]
        else:
            entry = ToolResults([])
            self._messages.insert(j, entry)
        entry.results.extend(results)
        if persist:
            self.save()

    def tool_results_of(self, model: ModelMessage) -> ToolResults | None:
        """The ToolResults entry immediately after a model, if any."""
        try:
            idx = self._messages.index(model)
        except ValueError:
            return None
        j = idx + 1
        if j < len(self._messages) and isinstance(self._messages[j], ToolResults):
            return self._messages[j]
        return None

    def insert(self, index: int, message: Message, persist: bool = True) -> None:
        """Insert at an explicit position (the system-prompt head insert)."""
        self._messages.insert(index, message)
        if persist:
            self.save()

    def remove(self, message: Message, persist: bool = True) -> None:
        """Remove a specific message object (e.g. an empty unpersisted model shell)."""
        try:
            self._messages.remove(message)
        except ValueError:
            pass
        if persist:
            self.save()

    def truncate(self, index: int) -> None:
        """Drop every message from ``index`` onward (inclusive)."""
        self._messages = self._messages[:index]
        self.save()

    def truncate_at(self, message_id: str) -> int:
        """Drop the message with the given id and everything after it.

        Returns the length of the kept prefix (what the broker's ``persisted_len``
        should become). Raises ValueError when the id is unknown.
        """
        idx = self.index_of(message_id)
        self.truncate(idx)
        return idx

    def index_of(self, message_id: str) -> int:
        for i, m in enumerate(self._messages):
            if m._id == message_id:
                return i
        raise ValueError(f"message id not found: {message_id}")

    def get_by_id(self, message_id: str) -> Message:
        return self._messages[self.index_of(message_id)]

    def user_seq_of(self, message: UserMessage) -> int:
        """Positional index of a real user message (excludes summaries/notices)."""
        count = 0
        for m in self._messages:
            if m is message:
                return count
            if isinstance(m, UserMessage) and not m.compact_summary and not m.is_mode_notification:
                count += 1
        raise ValueError("message not in list")

    # ── iteration / projection ───────────────────────────────────────────

    @property
    def raw(self) -> list[Message]:
        return self._messages

    def __len__(self) -> int:
        return len(self._messages)

    def __getitem__(self, index: int) -> Message:
        return self._messages[index]

    def __iter__(self):
        return iter(self._messages)

    def get_api_messages(self, image_dir: Path | None = None, enable_vl: bool = True) -> list[dict]:
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

        Truly empty model turns (no content, no tool_calls, and no think) are
        dropped so their surrounding user messages combine normally; a turn
        cancelled mid-stream with only half-streamed thinking is kept (the
        API-only projection substitutes a single space for the empty content so
        the reasoning survives -- see :meth:`ModelMessage._assistant_dict`).
        """
        start = 0
        for i, msg in enumerate(self._messages):
            if isinstance(msg, CompactMarker):
                start = i + 1  # start just past the marker

        msgs = [
            m for m in self._messages[start:]
            if not (isinstance(m, ModelMessage)
                    and not m.content and not m.tool_calls and not m.think)
        ]
        result: list[dict] = []
        i = 0
        while i < len(msgs):
            msg = msgs[i]
            if isinstance(msg, UserMessage):
                group: list[UserMessage] = [msg]
                i += 1
                while i < len(msgs) and isinstance(msgs[i], UserMessage):
                    group.append(msgs[i])
                    i += 1
                result.append(combine_new_messages(group, image_dir, enable_vl))
            else:
                result.extend(msg.to_api_dicts(image_dir=image_dir, enable_vl=enable_vl))
                i += 1
        return result

    def to_flat_dicts(self) -> list[dict]:
        """Flatten into the frontend's role-based dict list (init / /history)."""
        return [d for m in self._messages for d in m.to_frontend_dicts()]


class Turn:
    """One turn: the inputs fed to the model and the model's response.

    Inputs are the messages between the previous model output and this one:
    the previous batch's :class:`ToolResults` first, then any user/steering
    messages (that ordering is an invariant of the flat list). ``output`` is
    None for a trailing open turn (inputs accumulated, model never responded).
    """

    def __init__(self, inputs: list[Message], output: ModelMessage | None):
        self.inputs = inputs
        self.output = output


class TurnsView:
    """Turn-grouped view over a :class:`MessageList`.

    Read/compute operations (folding the flat list into :class:`Turn` objects)
    and domain operations such as ``revert`` that decompose into MessageList
    primitives. Never mutates the list directly -- all writes flow through the
    MessageList methods that maintain the ordering invariant.
    """

    def __init__(self, ml: MessageList):
        self._ml = ml

    @property
    def turns(self) -> list[Turn]:
        """Fold the flat list into turns. CompactMarkers belong to no turn."""
        turns: list[Turn] = []
        inputs: list[Message] = []
        for m in self._ml.raw:
            if isinstance(m, ModelMessage):
                turns.append(Turn(inputs=inputs, output=m))
                inputs = []
            elif isinstance(m, CompactMarker):
                continue
            else:
                inputs.append(m)
        if inputs:
            turns.append(Turn(inputs=inputs, output=None))
        return turns

    def find_user(self, message_id: str) -> UserMessage:
        """Locate a user message by id (raises ValueError if unknown / not a user)."""
        msg = self._ml.get_by_id(message_id)
        if not isinstance(msg, UserMessage):
            raise ValueError("message id does not reference a user message")
        return msg
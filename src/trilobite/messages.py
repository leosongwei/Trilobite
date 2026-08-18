from __future__ import annotations

"""Typed conversation messages (v3 storage model).

History is a *flat* list of :class:`Message` objects whose disk form (v3)
mirrors the OpenAI completions message sequence: every entry carries a
``type`` field (``system``/``user``/``model``/``tool_results``/``compact_marker``)
and an ``id`` field so messages can be addressed by id (e.g. ``revert``).

A model turn is split across two entries: a :class:`ModelMessage`
(think/content/calls) followed by one :class:`ToolResults` entry holding the
results of its calls. The ordering invariant *all results of a call batch
precede any later user message* is enforced structurally by
:class:`MessageList`'s append primitives (see ``history.py``), not by object
containment -- this is exactly the wire order the OpenAI-compatible API
requires.

Each object has three projections:

* ``to_api_dicts()`` -- OpenAI-compatible dicts sent to the LLM. A
  :class:`ModelMessage` projects to one ``assistant`` dict (tool calls in
  OpenAI shape); a :class:`ToolResults` projects to one ``tool`` dict per
  result.
* ``to_storage_dict()`` -- the v3 JSON shape persisted to ``history.json``.
* ``to_frontend_dicts()`` -- flat role-based dicts for the ``init`` SSE
  snapshot and the ``/history`` endpoint. The frontend protocol stays
  role-based (``assistant``/``tool``) while storage uses
  ``model``/``tool_results``.

Legacy loads: v1 (bare flat dict array) and v2 (``{"version": 2}`` with
nested ``assistant.tool_results``) are converted into the same flat typed
list, assigning ids as they go; the first save rewrites the file as v3
(lazy upgrade, like the v1->v2 migration before it).
"""

import base64
import uuid
from pathlib import Path
from typing import Any


def new_id() -> str:
    """Compact id for a message: 48 bits of uuid4 hex (unique within a session)."""
    return uuid.uuid4().hex[:12]


class Message:
    """Base class for typed conversation messages. Every message carries a compact id."""

    def __init__(self, id: str = ""):
        self._id = id or new_id()

    def to_api_dicts(self, image_dir: Path | None = None, enable_vl: bool = True) -> list[dict]:
        raise NotImplementedError

    def to_storage_dict(self) -> dict:
        raise NotImplementedError

    def to_frontend_dicts(self) -> list[dict]:
        raise NotImplementedError


class SystemMessage(Message):
    """The initial system prompt, or a rebuilt one that is not a compaction boundary."""

    def __init__(self, content: str, id: str = ""):
        super().__init__(id)
        self.content = content

    def to_api_dicts(self, image_dir: Path | None = None, enable_vl: bool = True) -> list[dict]:
        return [{"role": "system", "content": self.content}]

    def to_storage_dict(self) -> dict:
        return {"type": "system", "id": self._id, "content": self.content}

    def to_frontend_dicts(self) -> list[dict]:
        return [{"role": "system", "id": self._id, "content": self.content}]


class CompactMarker(Message):
    """A compaction boundary: a pure marker that restarts the API context.

    Carries no content itself -- a fresh :class:`SystemMessage` with the
    rebuilt prompt follows it. Everything before the last ``CompactMarker`` is
    dropped from ``get_api_messages()`` (which starts just past it) but kept in
    persisted history (the frontend renders it as a divider).
    """

    def to_api_dicts(self, image_dir: Path | None = None, enable_vl: bool = True) -> list[dict]:
        return []

    def to_storage_dict(self) -> dict:
        return {"type": "compact_marker", "id": self._id}

    def to_frontend_dicts(self) -> list[dict]:
        return [{"role": "system", "compact_marker": True}]


class Image:
    """An image attached to a user message.

    The actual bytes are stored in the session's ``images/`` directory under a
    hash-style filename; the message object only keeps the metadata needed to
    rebuild the API payload and to render the reference in the frontend.
    """

    def __init__(self, filename: str, mime_type: str, original_name: str = "", date: str = ""):
        self.filename = filename
        self.mime_type = mime_type
        self.original_name = original_name or filename
        self.date = date

    def to_storage_dict(self) -> dict:
        d = {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "original_name": self.original_name,
        }
        if self.date:
            d["date"] = self.date
        return d

    def to_frontend_dict(self) -> dict:
        d = {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "original_name": self.original_name,
        }
        if self.date:
            d["date"] = self.date
        return d

    def to_api_part(self, image_dir: Path) -> dict:
        """Build an OpenAI-compatible image_url content part from the stored file."""
        path = image_dir / self.filename
        data = path.read_bytes()
        b64 = base64.b64encode(data).decode()
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{self.mime_type};base64,{b64}",
                "detail": "auto",
            },
        }


class UserMessage(Message):
    def __init__(
        self,
        content: str,
        compact_summary: bool = False,
        is_compact_prompt: bool = False,
        is_mode_notification: bool = False,
        images: list[Image] | None = None,
        id: str = "",
    ):
        super().__init__(id)
        self.content = content
        self.compact_summary = compact_summary
        # Marks the user message that requests a handoff summary. It still
        # counts as a user turn (so the run loop continues into the compact
        # turn) but lets _finalize_compaction locate it and collect the real
        # steering messages that arrived after it.
        self.is_compact_prompt = is_compact_prompt
        # Marks a plan/build mode-change notice. It is persisted in history as
        # a user message (so the API prefix grows monotonically and stays
        # cacheable) rather than transiently spliced into the request. It is
        # not a real user turn: the frontend hides it and it is excluded from
        # user_seq, like compact summaries.
        self.is_mode_notification = is_mode_notification
        self.images = images or []

    def to_api_dicts(self, image_dir: Path | None = None, enable_vl: bool = True) -> list[dict]:
        if not self.images or not enable_vl:
            return [{"role": "user", "content": self.content}]
        if image_dir is None:
            raise ValueError("UserMessage with images requires an image_dir to build API payload")
        parts: list[dict] = []
        if self.content:
            parts.append({"type": "text", "text": self.content})
        for img in self.images:
            parts.append(img.to_api_part(image_dir))
        return [{"role": "user", "content": parts}]

    def to_storage_dict(self) -> dict:
        d: dict = {"type": "user", "id": self._id, "content": self.content}
        if self.images:
            d["images"] = [img.to_storage_dict() for img in self.images]
        if self.compact_summary:
            d["compact_summary"] = True
        if self.is_compact_prompt:
            d["is_compact_prompt"] = True
        if self.is_mode_notification:
            d["is_mode_notification"] = True
        return d

    def to_frontend_dicts(self) -> list[dict]:
        d: dict = {"role": "user", "id": self._id, "content": self.content}
        if self.images:
            d["images"] = [img.to_frontend_dict() for img in self.images]
        if self.compact_summary:
            d["compact_summary"] = True
        if self.is_compact_prompt:
            d["is_compact_prompt"] = True
        if self.is_mode_notification:
            d["is_mode_notification"] = True
        return [d]


class ToolCall:
    """A single tool call. ``arguments`` accumulates as the stream drains.

    The ``id`` is the API-generated call id (``call_xxx``) referenced by
    :class:`ToolResult` -- distinct from a message's ``_id``.
    """

    def __init__(self, id: str = "", name: str = "", arguments: str = ""):
        self.id = id
        self.name = name
        self.arguments = arguments

    def to_api_dict(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }

    # Storage uses the same OpenAI-compatible shape so the disk format stays
    # close to the API and no conversion is needed on load.
    to_storage_dict = to_api_dict


class ToolResult:
    def __init__(self, tool_call_id: str, content: str, diff: list | None = None):
        self.tool_call_id = tool_call_id
        self.content = content
        self.diff = diff

    def to_api_dict(self) -> dict:
        return {"role": "tool", "tool_call_id": self.tool_call_id, "content": self.content}

    def to_storage_dict(self) -> dict:
        d: dict = {"tool_call_id": self.tool_call_id, "content": self.content}
        if self.diff is not None:
            d["diff"] = self.diff
        return d

    def to_frontend_dict(self) -> dict:
        d: dict = {"role": "tool", "tool_call_id": self.tool_call_id, "content": self.content}
        if self.diff is not None:
            d["diff"] = self.diff
        return d


class ModelMessage(Message):
    """One model response: think + content + tool_calls.

    Appended empty at the start of a turn and mutated as the stream drains.
    The results of its calls live in a separate :class:`ToolResults` entry
    right after it -- the flat storage/appendix list keeps them adjacent, so a
    steering user message can never split a call batch from its results.
    """

    def __init__(self, think: str = "", content: str = "", tool_calls: list[ToolCall] | None = None, id: str = ""):
        super().__init__(id)
        self.think = think
        self.content = content
        self.tool_calls = tool_calls if tool_calls is not None else []

    def _assistant_dict(self, for_api: bool = False) -> dict:
        d: dict = {"role": "assistant"}
        if self.tool_calls:
            if self.content:
                d["content"] = self.content
            if self.think:
                d["reasoning_content"] = self.think
            d["tool_calls"] = [tc.to_api_dict() for tc in self.tool_calls]
        else:
            # Plain-text turn: content is always present (even "") so the API
            # never sees a missing content key (which glm-5.2 rejects with 400).
            # But an *empty* content string makes glm-5.2 silently drop the
            # whole assistant message -- including its reasoning_content -- so
            # the next turn cannot inherit a half-streamed thinking from a
            # cancelled turn. For the API projection only, substitute a single
            # space when content is empty so the reasoning survives. The
            # frontend and storage keep the real (empty) content.
            content = self.content
            if for_api and not content:
                content = " "
            d["content"] = content
            if self.think:
                d["reasoning_content"] = self.think
        return d

    def to_api_dicts(self, image_dir: Path | None = None, enable_vl: bool = True) -> list[dict]:
        return [self._assistant_dict(for_api=True)]

    def to_storage_dict(self) -> dict:
        d: dict = {"type": "model", "id": self._id, "think": self.think, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [tc.to_storage_dict() for tc in self.tool_calls]
        return d

    def to_frontend_dicts(self) -> list[dict]:
        return [self._assistant_dict()]


class ToolResults(Message):
    """The results of one model turn's tool calls (one entry per call batch).

    Lives immediately after its :class:`ModelMessage` in the flat list; the
    ordering invariant ("results before any later user message") makes this
    adjacency structurally guaranteed by :class:`MessageList`. Projects to one
    ``tool`` dict per result (the ``diff`` field is frontend-only).
    """

    def __init__(self, results: list[ToolResult] | None = None, id: str = ""):
        super().__init__(id)
        self.results = results if results is not None else []

    def to_api_dicts(self, image_dir: Path | None = None, enable_vl: bool = True) -> list[dict]:
        return [tr.to_api_dict() for tr in self.results]

    def to_storage_dict(self) -> dict:
        return {
            "type": "tool_results",
            "id": self._id,
            "results": [tr.to_storage_dict() for tr in self.results],
        }

    def to_frontend_dicts(self) -> list[dict]:
        return [tr.to_frontend_dict() for tr in self.results]


# ── loading ─────────────────────────────────────────────────────────────────


def _tool_call_from_dict(tc: dict) -> ToolCall:
    """Parse a tool_call dict in either the OpenAI shape or the legacy flat shape."""
    fn = tc.get("function") or {}
    return ToolCall(
        id=tc.get("id", ""),
        name=fn.get("name", "") if fn else tc.get("name", ""),
        arguments=fn.get("arguments", "") if fn else tc.get("arguments", ""),
    )


def from_v1(flat: list[dict]) -> list[Message]:
    """Convert a legacy flat dict array (v1 history.json) into typed messages.

    An ``assistant`` dict plus its consecutive ``tool`` dicts become a
    :class:`ModelMessage` followed by one :class:`ToolResults` entry,
    reconstructing the flat call-batch shape. Orphan tool messages (no
    preceding assistant) are dropped -- they should not occur in well-formed
    histories.
    """
    result: list[Message] = []
    pending_model: ModelMessage | None = None
    pending_results: ToolResults | None = None
    for d in flat:
        role = d.get("role")
        if role == "system":
            pending_model = None
            pending_results = None
            if d.get("compact_marker"):
                # v1 stored the rebuilt prompt inside the marker; split it into
                # a contentless CompactMarker followed by a SystemMessage.
                result.append(CompactMarker())
                result.append(SystemMessage(d.get("content", "")))
            else:
                result.append(SystemMessage(d.get("content", "")))
        elif role == "user":
            pending_model = None
            pending_results = None
            result.append(UserMessage(
                d.get("content", ""),
                compact_summary=bool(d.get("compact_summary")),
            ))
        elif role == "assistant":
            pending_model = ModelMessage(
                think=d.get("reasoning_content", "") or "",
                content=d.get("content", "") or "",
                tool_calls=[_tool_call_from_dict(tc) for tc in d.get("tool_calls", [])],
            )
            result.append(pending_model)
            pending_results = None
        elif role == "tool":
            if pending_model is not None:
                if pending_results is None:
                    pending_results = ToolResults([])
                    result.append(pending_results)
                pending_results.results.append(ToolResult(
                    tool_call_id=d.get("tool_call_id", ""),
                    content=d.get("content", ""),
                    diff=d.get("diff"),
                ))
    return result


def from_v2(messages: list[dict]) -> list[Message]:
    """Convert v2 entries (``{"version": 2, "messages": [...]}``) to typed messages.

    A v2 ``assistant`` entry carries its ``tool_results`` nested; it is split
    into a :class:`ModelMessage` followed by a :class:`ToolResults` entry.
    """
    result: list[Message] = []
    for d in messages:
        t = d.get("type")
        if t == "system":
            result.append(SystemMessage(d.get("content", "")))
        elif t == "compact_marker":
            result.append(CompactMarker())
        elif t == "user":
            result.append(UserMessage(
                d.get("content", ""),
                compact_summary=bool(d.get("compact_summary")),
                is_compact_prompt=bool(d.get("is_compact_prompt")),
                is_mode_notification=bool(d.get("is_mode_notification")),
                images=[
                    Image(
                        img.get("filename", ""),
                        img.get("mime_type", ""),
                        img.get("original_name", ""),
                        img.get("date", ""),
                    )
                    for img in d.get("images", [])
                ],
            ))
        elif t == "assistant":
            result.append(ModelMessage(
                think=d.get("thinking", "") or "",
                content=d.get("content", "") or "",
                tool_calls=[
                    ToolCall(tc.get("id", ""), tc.get("name", ""), tc.get("arguments", ""))
                    for tc in d.get("tool_calls", [])
                ],
            ))
            results = [
                ToolResult(tr.get("tool_call_id", ""), tr.get("content", ""), tr.get("diff"))
                for tr in d.get("tool_results", [])
            ]
            if results:
                result.append(ToolResults(results))
    return result


def message_from_storage(d: dict) -> Message:
    """Reconstruct a typed message from a v3 storage dict."""
    t = d.get("type")
    mid = d.get("id", "")
    if t == "system":
        return SystemMessage(d.get("content", ""), id=mid)
    if t == "compact_marker":
        return CompactMarker(id=mid)
    if t == "user":
        return UserMessage(
            d.get("content", ""),
            compact_summary=bool(d.get("compact_summary")),
            is_compact_prompt=bool(d.get("is_compact_prompt")),
            is_mode_notification=bool(d.get("is_mode_notification")),
            images=[
                Image(
                    img.get("filename", ""),
                    img.get("mime_type", ""),
                    img.get("original_name", ""),
                    img.get("date", ""),
                )
                for img in d.get("images", [])
            ],
            id=mid,
        )
    if t == "model":
        return ModelMessage(
            think=d.get("think", "") or "",
            content=d.get("content", "") or "",
            tool_calls=[_tool_call_from_dict(tc) for tc in d.get("tool_calls", [])],
            id=mid,
        )
    if t == "tool_results":
        return ToolResults(
            [ToolResult(tr.get("tool_call_id", ""), tr.get("content", ""), tr.get("diff"))
             for tr in d.get("results", [])],
            id=mid,
        )
    # Unknown type: best-effort fallback so a malformed entry never breaks load.
    return SystemMessage(str(d), id=mid)


def combine_new_messages(messages: list[UserMessage], image_dir: Path | None = None, enable_vl: bool = True) -> dict:
    """Combine consecutive user messages into one API user dict.

    A single message is passed through unchanged. Multiple messages are joined
    with a ``<multi_message/>`` separator before each one, so the model can
    tell these are several distinct user inputs (e.g. steering messages, or a
    steering message followed by a compact prompt) rather than one typed
    message. The system prompt explains the marker.

    When any message carries images and ``enable_vl`` is true, ``content``
    becomes a list of OpenAI-style parts (text + image_url). ``image_dir`` is
    required in that case so the stored image files can be read and
    base64-encoded. When ``enable_vl`` is false, image parts are dropped but
    the text is kept, so the same history can be reused on a non-vision model
    without deleting the image metadata from the persisted history.
    """
    has_images = enable_vl and any(m.images for m in messages)
    if not has_images:
        if len(messages) == 1:
            return {"role": "user", "content": messages[0].content}
        parts = [f"<multi_message/>\n{m.content}" for m in messages]
        return {"role": "user", "content": "\n".join(parts)}

    if image_dir is None:
        raise ValueError("combine_new_messages with images requires image_dir")
    parts: list[dict] = []
    for i, m in enumerate(messages):
        prefix = "<multi_message/>\n" if i > 0 else ""
        if m.content:
            parts.append({"type": "text", "text": prefix + m.content})
        elif i > 0:
            parts.append({"type": "text", "text": prefix})
        for img in m.images:
            parts.append(img.to_api_part(image_dir))
    if not parts:
        return {"role": "user", "content": ""}
    return {"role": "user", "content": parts}
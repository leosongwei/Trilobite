from __future__ import annotations

"""Typed conversation messages.

History stores :class:`Message` objects (the v2 in-memory representation).
Each object can be projected three ways:

* ``to_api_dicts()``      -- OpenAI-compatible dicts sent to the LLM. An
  :class:`AssistantMessage` expands to ``[assistant, tool, tool, ...]``.
* ``to_storage_dict()``   -- the v2 JSON shape persisted to ``history.json``.
* ``to_frontend_dicts()`` -- flat v1-compatible dicts for the ``init`` SSE
  snapshot, so the frontend protocol stays unchanged.

The key design choice is that :class:`AssistantMessage` is *self-contained*:
``tool_results`` live inside it instead of being separate history entries. A
steering :class:`UserMessage` can therefore only land *after* a whole
assistant turn, never between an ``assistant(tool_calls)`` and its tool
results -- which keeps the API message sequence valid.
"""

import base64
from pathlib import Path
from typing import Any


class Message:
    """Base class for typed conversation messages."""

    def to_api_dicts(self, image_dir: Path | None = None, enable_vl: bool = True) -> list[dict]:
        raise NotImplementedError

    def to_storage_dict(self) -> dict:
        raise NotImplementedError

    def to_frontend_dicts(self) -> list[dict]:
        raise NotImplementedError


class SystemMessage(Message):
    """The initial system prompt (or a rebuilt one that is not a compaction boundary)."""

    def __init__(self, content: str):
        self.content = content

    def to_api_dicts(self, image_dir: Path | None = None, enable_vl: bool = True) -> list[dict]:
        return [{"role": "system", "content": self.content}]

    def to_storage_dict(self) -> dict:
        return {"type": "system", "content": self.content}

    def to_frontend_dicts(self) -> list[dict]:
        return [{"role": "system", "content": self.content}]


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
        return {"type": "compact_marker"}

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
        images: list[Image] | None = None,
    ):
        self.content = content
        self.compact_summary = compact_summary
        # Marks the user message that requests a handoff summary. It still
        # counts as a user turn (so the run loop continues into the compact
        # turn) but lets _finalize_compaction locate it and collect the real
        # steering messages that arrived after it.
        self.is_compact_prompt = is_compact_prompt
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
        d: dict = {"type": "user", "content": self.content}
        if self.images:
            d["images"] = [img.to_storage_dict() for img in self.images]
        if self.compact_summary:
            d["compact_summary"] = True
        if self.is_compact_prompt:
            d["is_compact_prompt"] = True
        return d

    def to_frontend_dicts(self) -> list[dict]:
        d: dict = {"role": "user", "content": self.content}
        if self.images:
            d["images"] = [img.to_frontend_dict() for img in self.images]
        if self.compact_summary:
            d["compact_summary"] = True
        if self.is_compact_prompt:
            d["is_compact_prompt"] = True
        return [d]


class ToolCall:
    """A single tool call. ``arguments`` accumulates as the stream drains."""

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

    def to_storage_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


class ToolResult:
    def __init__(self, tool_call_id: str, content: str, diff: list | None = None):
        self.tool_call_id = tool_call_id
        self.content = content
        self.diff = diff

    def to_storage_dict(self) -> dict:
        d: dict = {"tool_call_id": self.tool_call_id, "content": self.content}
        if self.diff is not None:
            d["diff"] = self.diff
        return d


class AssistantMessage(Message):
    """One agent turn: thinking + content + tool_calls + tool_results.

    Appended empty at the start of a turn and mutated as the stream drains and
    tools execute. Self-contained so a steering user message can never split an
    ``assistant(tool_calls)`` from its tool results.
    """

    def __init__(
        self,
        thinking: str = "",
        content: str = "",
        tool_calls: list[ToolCall] | None = None,
        tool_results: list[ToolResult] | None = None,
    ):
        self.thinking = thinking
        self.content = content
        self.tool_calls = tool_calls if tool_calls is not None else []
        self.tool_results = tool_results if tool_results is not None else []

    def _assistant_dict(self, for_api: bool = False) -> dict:
        d: dict = {"role": "assistant"}
        if self.tool_calls:
            if self.content:
                d["content"] = self.content
            if self.thinking:
                d["reasoning_content"] = self.thinking
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
            if self.thinking:
                d["reasoning_content"] = self.thinking
        return d

    def to_api_dicts(self, image_dir: Path | None = None, enable_vl: bool = True) -> list[dict]:
        result: list[dict] = [self._assistant_dict(for_api=True)]
        for tr in self.tool_results:
            # diff is frontend-only; never sent to the API.
            result.append({
                "role": "tool",
                "tool_call_id": tr.tool_call_id,
                "content": tr.content,
            })
        return result

    def to_storage_dict(self) -> dict:
        d: dict = {"type": "assistant", "thinking": self.thinking, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [tc.to_storage_dict() for tc in self.tool_calls]
        if self.tool_results:
            d["tool_results"] = [tr.to_storage_dict() for tr in self.tool_results]
        return d

    def to_frontend_dicts(self) -> list[dict]:
        result: list[dict] = [self._assistant_dict()]
        for tr in self.tool_results:
            td: dict = {
                "role": "tool",
                "tool_call_id": tr.tool_call_id,
                "content": tr.content,
            }
            if tr.diff is not None:
                td["diff"] = tr.diff
            result.append(td)
        return result


# ── loading ─────────────────────────────────────────────────────────────────


def _tool_call_from_v1(tc: dict) -> ToolCall:
    fn = tc.get("function", {}) or {}
    return ToolCall(
        id=tc.get("id", ""),
        name=fn.get("name", ""),
        arguments=fn.get("arguments", ""),
    )


def from_v1(flat: list[dict]) -> list[Message]:
    """Merge a legacy flat dict array (v1 history.json) into typed objects.

    A ``tool`` dict is folded into the preceding ``assistant(tool_calls)``
    message as a :class:`ToolResult`, reconstructing the self-contained
    :class:`AssistantMessage` shape.
    """
    messages: list[Message] = []
    pending: AssistantMessage | None = None
    for d in flat:
        role = d.get("role")
        if role == "system":
            pending = None
            if d.get("compact_marker"):
                # v1 stored the rebuilt prompt inside the marker; split it into
                # a contentless CompactMarker followed by a SystemMessage.
                messages.append(CompactMarker())
                messages.append(SystemMessage(d.get("content", "")))
            else:
                messages.append(SystemMessage(d.get("content", "")))
        elif role == "user":
            pending = None
            messages.append(UserMessage(
                d.get("content", ""),
                compact_summary=bool(d.get("compact_summary")),
            ))
        elif role == "assistant":
            pending = AssistantMessage(
                thinking=d.get("reasoning_content", "") or "",
                content=d.get("content", "") or "",
                tool_calls=[_tool_call_from_v1(tc) for tc in d.get("tool_calls", [])],
                tool_results=[],
            )
            messages.append(pending)
        elif role == "tool":
            tr = ToolResult(
                tool_call_id=d.get("tool_call_id", ""),
                content=d.get("content", ""),
                diff=d.get("diff"),
            )
            if pending is not None:
                pending.tool_results.append(tr)
            # An orphan tool message (no preceding assistant) is dropped --
            # it should not occur in well-formed histories.
    return messages


def message_from_storage(d: dict) -> Message:
    """Reconstruct a typed message from a v2 storage dict."""
    t = d.get("type")
    if t == "system":
        return SystemMessage(d.get("content", ""))
    if t == "compact_marker":
        return CompactMarker()
    if t == "user":
        images = [
            Image(
                img.get("filename", ""),
                img.get("mime_type", ""),
                img.get("original_name", ""),
                img.get("date", ""),
            )
            for img in d.get("images", [])
        ]
        return UserMessage(
            d.get("content", ""),
            compact_summary=bool(d.get("compact_summary")),
            is_compact_prompt=bool(d.get("is_compact_prompt")),
            images=images or None,
        )
    if t == "assistant":
        return AssistantMessage(
            thinking=d.get("thinking", "") or "",
            content=d.get("content", "") or "",
            tool_calls=[
                ToolCall(id=tc.get("id", ""), name=tc.get("name", ""), arguments=tc.get("arguments", ""))
                for tc in d.get("tool_calls", [])
            ],
            tool_results=[
                ToolResult(
                    tool_call_id=tr.get("tool_call_id", ""),
                    content=tr.get("content", ""),
                    diff=tr.get("diff"),
                )
                for tr in d.get("tool_results", [])
            ],
        )
    # Unknown type: best-effort fallback so a malformed entry never breaks load.
    return SystemMessage(str(d))


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

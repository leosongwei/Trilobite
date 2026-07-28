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

from typing import Any


class Message:
    """Base class for typed conversation messages."""

    def to_api_dicts(self) -> list[dict]:
        raise NotImplementedError

    def to_storage_dict(self) -> dict:
        raise NotImplementedError

    def to_frontend_dicts(self) -> list[dict]:
        raise NotImplementedError


class SystemMessage(Message):
    """The initial system prompt (or a rebuilt one that is not a compaction boundary)."""

    def __init__(self, content: str):
        self.content = content

    def to_api_dicts(self) -> list[dict]:
        return [{"role": "system", "content": self.content}]

    def to_storage_dict(self) -> dict:
        return {"type": "system", "content": self.content}

    def to_frontend_dicts(self) -> list[dict]:
        return [{"role": "system", "content": self.content}]


class CompactMarker(Message):
    """A compaction boundary: a rebuilt system prompt that restarts the API context.

    Everything before the last ``CompactMarker`` is dropped from
    ``get_api_messages()`` but kept in persisted history (for the frontend).
    Itself projects as a plain ``system`` message to the API.
    """

    def __init__(self, content: str):
        self.content = content

    def to_api_dicts(self) -> list[dict]:
        return [{"role": "system", "content": self.content}]

    def to_storage_dict(self) -> dict:
        return {"type": "compact_marker", "content": self.content}

    def to_frontend_dicts(self) -> list[dict]:
        return [{"role": "system", "content": self.content, "compact_marker": True}]


class UserMessage(Message):
    def __init__(self, content: str, compact_summary: bool = False):
        self.content = content
        self.compact_summary = compact_summary

    def to_api_dicts(self) -> list[dict]:
        return [{"role": "user", "content": self.content}]

    def to_storage_dict(self) -> dict:
        d: dict = {"type": "user", "content": self.content}
        if self.compact_summary:
            d["compact_summary"] = True
        return d

    def to_frontend_dicts(self) -> list[dict]:
        d: dict = {"role": "user", "content": self.content}
        if self.compact_summary:
            d["compact_summary"] = True
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

    def _assistant_dict(self) -> dict:
        d: dict = {"role": "assistant"}
        if self.tool_calls:
            if self.content:
                d["content"] = self.content
            if self.thinking:
                d["reasoning_content"] = self.thinking
            d["tool_calls"] = [tc.to_api_dict() for tc in self.tool_calls]
        else:
            # Plain-text turn: content is always present (even ""), matching
            # the legacy done-branch shape the API and frontend expect.
            d["content"] = self.content
            if self.thinking:
                d["reasoning_content"] = self.thinking
        return d

    def to_api_dicts(self) -> list[dict]:
        result: list[dict] = [self._assistant_dict()]
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
                messages.append(CompactMarker(d.get("content", "")))
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
        return CompactMarker(d.get("content", ""))
    if t == "user":
        return UserMessage(d.get("content", ""), compact_summary=bool(d.get("compact_summary")))
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

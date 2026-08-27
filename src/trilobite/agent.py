from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from typing import Any

from src.trilobite.broker import StreamBroker
from src.trilobite.compaction import should_compact, build_compact_prompt
from src.trilobite.config import (
    DEFAULT_MAX_CONTEXT_TOKENS,
    DEFAULT_MAX_TOKENS,
    get_default_model_name,
    get_model,
)
from src.trilobite.file_access import normalize_dir, normalize_dirs
from src.trilobite.history import MessageList, TurnsView
from src.trilobite.messages import (
    CompactMarker,
    Image,
    ModelMessage,
    SystemMessage,
    ToolCall,
    ToolResult,
    UserMessage,
)
from src.trilobite.prompts import (
    IMAGE_READ_PROMPT,
    SYSTEM_PROMPT,
    subagent_system_prompt,
)
from src.trilobite.permission import (
    AgentPermission,
    BuildModePermission,
    ExploreSubagentPermission,
    GeneralSubagentPermission,
    PlanModePermission,
)
from src.trilobite.skills import discover_skills, format_skill_listing
from src.trilobite.timer import parse_sleep_until, sleep_placeholder
from src.trilobite.tools.bash import kill_process_group, truncate_output
from src.trilobite.tool_call import execute_tool

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# ── opencode-compatible session ID ──────────────────────────────────────────

def _generate_session_id() -> str:
    """Generate an opencode-style session ID: ses_ + 26 chars."""
    ts_ms = int(time.time() * 1000)
    current = ts_ms << 12  # * 4096, counter 0 is fine
    value = ~current & 0xFFFFFFFFFFFFFFFF  # 64-bit NOT, descending
    time_hex = "".join(f"{(value >> (40 - 8 * i)) & 0xFF:02x}" for i in range(6))
    rand = "".join(_CHARS[b % 62] for b in os.urandom(14))
    return f"ses_{time_hex}{rand}"


# ── thin SSE-chunk wrappers (mirror OpenAI SDK shapes) ──────────────────────

@dataclass
class _Function:
    name: str = ""
    arguments: str = ""

@dataclass
class _ToolCall:
    index: int = 0
    id: str = ""
    function: _Function = field(default_factory=_Function)

@dataclass
class _Delta:
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[_ToolCall] | None = None

@dataclass
class _Choice:
    delta: _Delta = field(default_factory=_Delta)
    finish_reason: str | None = None

@dataclass
class _Usage:
    total_tokens: int = 0

@dataclass
class _StreamChunk:
    choices: list[_Choice] = field(default_factory=list)
    usage: _Usage | None = None


class _StreamAttemptError(Exception):
    """A chat-completion stream attempt failed.

    Unified failure signal for every kind of bad stream: any HTTP error
    (status or transport), a premature disconnect (stream ended without a
    ``[DONE]`` marker and without a finish reason), and completions whose
    output contradicts the ``finish_reason`` declaration (half-baked tool
    calls, stop without content, truncated or empty output). The turn's
    partial output is discarded and the identical request is retried; the
    exception only surfaces after all attempts are exhausted.

    ``retryable=False`` marks failures that are deterministic and therefore
    not worth re-issuing: a ``length`` truncation replays identically (same
    request, same ``max_tokens`` budget, same thinking length), so the turn
    fails immediately instead of burning the retry budget.
    """

    def __init__(self, reason: str, *, retryable: bool = True):
        super().__init__(reason)
        self.retryable = retryable

    @property
    def status_code(self) -> int | None:
        cause = self.__cause__
        if isinstance(cause, httpx.HTTPStatusError):
            return cause.response.status_code
        return None

    @property
    def body(self) -> dict | None:
        cause = self.__cause__
        if isinstance(cause, httpx.HTTPStatusError):
            try:
                return cause.response.json()
            except Exception:
                return None
        return None


# Provider error → short banner label (shown in the retry status event).
_STREAM_ERROR_LABELS = {
    httpx.ConnectError: "connection failed",
    httpx.ConnectTimeout: "connect timed out",
    httpx.ReadTimeout: "read timed out",
    httpx.WriteTimeout: "write timed out",
    httpx.ReadError: "connection interrupted",
    httpx.RemoteProtocolError: "connection interrupted",
    httpx.LocalProtocolError: "protocol error",
    httpx.DecodingError: "decode failed",
    httpx.PoolTimeout: "pool timeout",
}


def _stream_error_label(err: Exception) -> str:
    """Short human-readable label for a stream transport error."""
    return _STREAM_ERROR_LABELS.get(type(err), type(err).__name__)

PLAN_MODE_NOTIFICATION = (
    '<modeswitch mode="plan">\n'
    "You are now in plan mode (read-only analysis).\n"
    "The following tools are blocked and will be rejected if called: edit, write.\n"
    "All other tools remain available: read, glob, grep, bash, TodoList, exit_plan_mode, task, sleep_until.\n"
    "Note: in plan mode the task tool may only spawn explore (read-only) subagents.\n"
    "Focus on exploring, analyzing, and planning. To make file changes, call exit_plan_mode to request switching to build mode.\n"
    "</modeswitch>"
)

BUILD_MODE_NOTIFICATION = (
    '<modeswitch mode="build">\n'
    "You are now in build mode (full access).\n"
    "All tools are available: read, glob, grep, edit, write, bash, TodoList, task, sleep_until.\n"
    "(exit_plan_mode is a no-op in build mode and will be rejected if called.)\n"
    "You may make file changes, run shell commands, and use your full arsenal of tools.\n"
    "</modeswitch>"
)


# ── httpx-based OpenAI-compatible streaming chat completions ────────────────

def _build_request_headers(
    api_key: str,
    *,
    pretend_to_be_opencode: bool = True,
    stream: bool = False,
) -> dict:
    """Build request headers for an OpenAI-compatible chat completion.

    When ``pretend_to_be_opencode`` is false we use a minimal standard header
    set.  An empty ``api_key`` omits the ``Authorization`` header entirely,
    which is needed for local servers (e.g. llama.cpp) that do not expect a
    ``Bearer`` token.
    """
    headers: dict[str, str] = {
        "Content-Type": "application/json",
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    # In opencode mode we always send Authorization (even with an empty key)
    # for backwards compatibility.  In plain mode we only send it when a key
    # is configured, which lets local servers like llama.cpp omit the header.
    if pretend_to_be_opencode or api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def _chat_completion_stream(
    http: httpx.AsyncClient,
    api_key: str,
    pretend_to_be_opencode: bool,
    body: dict,
    log: logging.Logger | None = None,
):
    """Stream SSE chunks from an OpenAI-compatible chat/completions endpoint."""
    headers = _build_request_headers(api_key, pretend_to_be_opencode=pretend_to_be_opencode, stream=True)
    if log:
        log.info("STREAM request model=%s messages=%d tools=%d reasoning=%s max_tokens=%s",
                 body.get("model"), len(body.get("messages", [])),
                 len(body.get("tools", []) or []), body.get("reasoning_effort"), body.get("max_tokens"))
    chunk_count = 0
    finish_reasons: list[str | None] = []
    try:
        async with http.stream(
            "POST",
            "/chat/completions",
            json=body,
            headers=headers,
        ) as response:
            if log:
                log.info("STREAM response status=%s content-type=%s",
                         response.status_code, response.headers.get("content-type"))
            response.raise_for_status()
            done_seen = False
            async for line in response.aiter_lines():
                if log:
                    log.debug("STREAM raw> %s", line[:800])
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    done_seen = True
                    if log:
                        log.info("STREAM [DONE] after %d chunks, finish_reasons=%s",
                                 chunk_count, finish_reasons)
                    break
                try:
                    data = json.loads(data_str)
                    choices = []
                    for c in data.get("choices", []):
                        d = c.get("delta", {})
                        tc_list = []
                        # Some providers (e.g. GLM via opencode zen) emit
                        # ``"tool_calls": null`` on every delta chunk. Unlike a
                        # missing key, ``dict.get(..., [])`` returns ``None``
                        # here (key present, value null) and iterating it
                        # raises TypeError. ``... or []`` covers both cases.
                        for t in (d.get("tool_calls") or []):
                            tc_list.append(_ToolCall(
                                index=t.get("index", 0),
                                id=t.get("id", ""),
                                function=_Function(
                                    name=t.get("function", {}).get("name", ""),
                                    arguments=t.get("function", {}).get("arguments", ""),
                                ),
                            ))
                        fr = c.get("finish_reason")
                        choices.append(_Choice(
                            delta=_Delta(
                                content=d.get("content"),
                                reasoning_content=d.get("reasoning_content"),
                                tool_calls=tc_list if tc_list else None,
                            ),
                            finish_reason=fr,
                        ))
                    usage_raw = data.get("usage")
                    usage = _Usage(total_tokens=usage_raw.get("total_tokens", 0)) if usage_raw else None
                    chunk_count += 1
                    if log:
                        for idx, ch in enumerate(choices):
                            dlt = ch.delta
                            log.debug("STREAM chunk#%d c[%d] finish=%s content=%d reasoning=%d tc=%d",
                                      chunk_count, idx, ch.finish_reason,
                                      len(dlt.content) if dlt.content else 0,
                                      len(dlt.reasoning_content) if dlt.reasoning_content else 0,
                                      len(dlt.tool_calls) if dlt.tool_calls else 0)
                            if ch.finish_reason:
                                finish_reasons.append(ch.finish_reason)
                    yield _StreamChunk(choices=choices, usage=usage)
                except json.JSONDecodeError:
                    if log:
                        log.warning("STREAM json decode error: %s", data_str[:200])
                    continue
            if not done_seen and not finish_reasons:
                # The connection closed without a [DONE] marker and without a
                # finish reason -- the provider died mid-stream (broken
                # thinking chain, truncated output). Retryable.
                if log:
                    log.warning("STREAM ended WITHOUT [DONE] after %d chunks, finish_reasons=%s",
                                chunk_count, finish_reasons)
                raise _StreamAttemptError("connection closed")
    except _StreamAttemptError:
        raise
    except asyncio.CancelledError:
        raise
    except httpx.HTTPStatusError as e:
        # Any HTTP error (503, 429, 400, ...) is worth a retry: flaky
        # providers degrade with transient errors, and the retry cap bounds
        # the damage of a genuinely broken request.
        if log:
            log.warning("STREAM HTTP error: %r", e)
        raise _StreamAttemptError(f"HTTP {e.response.status_code}") from e
    except httpx.HTTPError as e:
        if log:
            log.warning("STREAM transport error: %r", e)
        raise _StreamAttemptError(_stream_error_label(e)) from e
    except Exception as e:
        if log:
            log.exception("STREAM error: %r", e)
        raise _StreamAttemptError(type(e).__name__) from e


class Agent:
    def __init__(
        self,
        name: str,
        working_dir: str,
        session_dir: Path,
        config: dict[str, str],
        session_id: str | None = None,
        *,
        subagent_type: str | None = None,
        description: str | None = None,
        registry: dict[str, Agent] | None = None,
        parent: Agent | None = None,
        depth: int = 0,
        max_steps: int | None = None,
        sealed: bool = False,
        timer_service: Any = None,
        model_name: str | None = None,
    ):
        self.name = name
        self.working_dir = Path(working_dir).resolve()
        self.session_dir = session_dir
        self.config = config
        self._session_id = session_id or _generate_session_id()
        # The session's chosen model (display name from the ``models`` config
        # list). New sessions default to ``default_model``; a session restored
        # from disk carries its persisted choice. Subagents inherit the
        # parent's model at spawn time.
        model_def = get_model(config, model_name or get_default_model_name(config))
        self._model_name = model_def.name
        self._api_key = model_def.api_key
        self._api_url = model_def.api_url.rstrip("/")
        self._pretend_to_be_opencode = model_def.pretend_to_be_opencode
        base_headers: dict[str, str] = {}
        if self._pretend_to_be_opencode:
            base_headers = {
                "User-Agent": "opencode/1.18.4",
                "x-session-affinity": self._session_id,
                "X-Session-Id": self._session_id,
            }
        self._http = httpx.AsyncClient(
            base_url=self._api_url,
            headers=base_headers,
            timeout=httpx.Timeout(600, connect=10),
        )
        self.model = model_def.model
        self.enable_vl = model_def.enable_vl
        self._log = logging.getLogger(f"trilobite.agent.{name}")
        if not self._log.handlers:
            _fh = logging.FileHandler(self.session_dir / "agent.log", encoding="utf-8")
            _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._log.addHandler(_fh)
        self._log.setLevel(config.get("log_level", "WARNING"))
        self._log.propagate = False
        self.max_context_tokens = model_def.max_context
        self.max_tokens = model_def.max_tokens
        self.compaction_trigger_ratio = model_def.compaction_trigger_ratio
        self._extra_body = model_def.extra_body or {}
        # Subagents override the system prompt with the role prefix + guidance
        # and use a fixed role permission (never plan/build mode).
        self._subagent_type: str | None = subagent_type
        self._description: str = description or ""
        if subagent_type == "explore":
            role_prompt = subagent_system_prompt("explore")
        elif subagent_type == "general":
            role_prompt = subagent_system_prompt("general")
        else:
            role_prompt = SYSTEM_PROMPT
        # Prepend a dynamic environment block (working dir, git, platform)
        # so the model knows where it is and prefers relative paths over
        # guessed absolute paths that drift outside the workspace.
        self._is_git_repo = self._detect_git_repo()
        self._system_base = self._build_env_block() + "\n\n" + role_prompt
        # Append the available-skills listing (name/description/path only;
        # the full body loads on demand via the skill tool). Like the env
        # block and AGENTS.md, the listing is baked into the system message
        # at session start; the VLM block (below) depends on the model's
        # enable_vl and is re-evaluated on model switches.
        listing = format_skill_listing(
            discover_skills(self.working_dir, self.config.get("skill_dirs", []))
        )
        self._skills_listing = listing
        self.system_prompt = self._build_system_prompt()
        self.working_context = self._load_working_context()
        self.history = MessageList(session_dir / "history.json")
        self._turns = TurnsView(self.history)
        self._broker = StreamBroker(len(self.history.raw))
        self._token_count: int = 0
        self._token_covered: int = 0
        self._task: asyncio.Task | None = None
        # Continuation signals for the run loop. The loop runs another turn if
        # either (a) the previous turn produced tool_calls whose results the
        # model has not seen yet, or (b) a user message (start/steer) arrived
        # since the model last read history. ``_force_run`` is set after
        # compaction so the model gets one turn on the rebuilt context.
        # ``_need_compact`` requests the next turn produce a handoff summary;
        # tools are still advertised (for cache stability) but any call is
        # intercepted. The turn after that rebuilds the context.
        self._pending_tool_results: bool = False
        self._user_read_cursor: int = 0
        self._force_run: bool = False
        self._need_compact: bool = False
        if subagent_type == "explore":
            self._permission: AgentPermission = ExploreSubagentPermission()
        elif subagent_type == "general":
            self._permission: AgentPermission = GeneralSubagentPermission()
        else:
            self._permission: AgentPermission = BuildModePermission()
        self._last_notified_mode: bool | None = None
        self._additional_dirs: list[Path] = []
        # Global fixed allowed dirs from the config (``allowed_dirs``): granted
        # to every session, never persisted per session, never revocable from
        # the UI. Relative entries resolve against this session's working dir.
        self._global_dirs = normalize_dirs(
            self.config.get("allowed_dirs", []) or [], self.working_dir
        )
        self._plan_exit_event: asyncio.Event = asyncio.Event()
        self._plan_exit_approved: bool = False
        self._permission_event: asyncio.Event = asyncio.Event()
        self._permission_approved: bool = False
        self._permission_path: str = ""
        # ── subagent lifecycle ───────────────────────────────────────────
        self._registry: dict[str, Agent] | None = registry
        self._parent: Agent | None = parent
        self._depth: int = depth
        self._max_steps: int | None = max_steps
        self._sealed: bool = sealed
        self._interrupted: bool = False
        self._step_count: int = 0
        self._children: list[Agent] = []
        # The Popen of the bash command currently running in a worker thread
        # (None when idle). Set/cleared via _register_proc so interrupt() can
        # kill it instead of blocking until the command finishes on its own.
        self._current_proc = None
        # The event loop of the run coroutine, captured so worker-thread
        # callbacks (bash on_output) can schedule stream events back onto it
        # via asyncio.run_coroutine_threadsafe.
        self._loop = None
        # Per-tool-call buffer of streamed bash output lines (text, stream).
        # When a run is cancelled mid-command, the asyncio.to_thread future is
        # cancelled and execute_tool's return value is lost; this buffer lets
        # the CancelledError handler salvage the partial output the on_output
        # callback already collected. Keyed by tool_call_id.
        self._tool_output_buffer: dict[str, list[tuple[str, str]]] = {}
        self._initial_prompt: str = ""
        self._final_state: str = "completed"
        self._final_result: str = ""
        # The TimerService backing the sleep_until virtual tool (injected by
        # the server for primary agents; None for CLI/subagents -> the tool
        # returns an error). ``_sleeping_until`` holds the armed suspension's
        # target (epoch seconds) from the sleep_until call until the next run
        # starts: the run loop breaks on it, and any new run (timer wake-up or
        # an early user message) clears it in its prologue.
        self._timer_service = timer_service
        self._sleeping_until: float | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    async def chat_completion(self, messages: list[dict], stream: bool = False, tools: list[dict] | None = None):
        """Make a chat completion request. Returns parsed JSON for non-stream,
        or an async generator of _StreamChunk for stream."""
        body: dict = {"model": self.model, "messages": messages}
        if tools is not None:
            body["tools"] = tools
        body["stream"] = stream
        body["max_tokens"] = self.max_tokens
        # Model-specific extra fields (e.g. ``reasoning_effort``/``thinking``
        # for thinking models) are user-defined per model in ``extra_body``
        # and merged verbatim into the request body.
        body.update(self._extra_body)
        if stream:
            body["stream_options"] = {"include_usage": True}
            return _chat_completion_stream(
                self._http,
                self._api_key,
                self._pretend_to_be_opencode,
                body,
                log=self._log,
            )
        else:
            headers = _build_request_headers(
                self._api_key,
                pretend_to_be_opencode=self._pretend_to_be_opencode,
            )
            resp = await self._http.post("/chat/completions", json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def _drain_stream(self, stream, model: ModelMessage) -> str | None:
        """Consume one chat-completion stream into ``model``.

        Reasoning/text deltas are appended to the model message and forwarded
        to subscribers (``thinking``/``text`` events); tool-call fragments are
        accumulated into ``model.tool_calls``. Token usage carried by the
        stream is recorded on the agent. Nothing touches disk here -- the turn
        is persisted only after it is validated by the caller.

        Returns the last ``finish_reason`` seen on the stream (``None`` if the
        stream ended without one), which the caller uses to diagnose
        truncated/empty completions.

        Tool-call fragments are accumulated **per ``index``** (the chunk's
        ``tool_calls[0].index`` field): providers stream each parallel call's
        id/name once at its start and then only argument fragments, and while
        llama.cpp emits calls sequentially (each next call re-sends its id),
        others may interleave argument fragments of several calls. Keying the
        accumulator on the id alone would merge interleaved fragments into one
        call; keying on the index keeps each call's arguments separate, and
        the final list is emitted in index order. ``tool_stream`` events keep
        the old id-driven shape (a ``complete: True`` closes the previous
        bubble when a new id-bearing call starts streaming).
        """
        # index -> {id, name, args} accumulators.
        indexed: dict[int, dict[str, str]] = {}
        active_index: int | None = None
        last_finish: str | None = None

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None

            # Capture real token usage from stream
            if chunk.usage:
                self._token_count = chunk.usage.total_tokens
                self._token_covered = len(self.history)
                self._persist_token_count()

            if chunk.choices:
                fr = chunk.choices[0].finish_reason
                if fr:
                    last_finish = fr

            if delta is None:
                continue

            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                model.think += delta.reasoning_content
                await self._send_stream_event({"type": "thinking", "text": delta.reasoning_content})

            if delta.content:
                model.content += delta.content
                await self._send_stream_event({"type": "text", "text": delta.content})

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    entry = indexed.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        entry["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            entry["name"] = tc.function.name
                        if tc.function.arguments:
                            entry["args"] += tc.function.arguments
                    if tc.id and entry["name"] and active_index != tc.index:
                        # A new id-bearing call starts streaming: close the
                        # previous bubble (if any) and open this one.
                        if active_index is not None:
                            prev = indexed[active_index]
                            await self._send_stream_event({
                                "type": "tool_stream",
                                "tool_name": prev["name"],
                                "args": prev["args"],
                                "complete": True,
                            })
                        active_index = tc.index
                        await self._send_stream_event({
                            "type": "tool_stream",
                            "tool_name": entry["name"],
                            "args": "",
                            "complete": False,
                        })
                    elif (
                        tc.index == active_index
                        and entry["name"]
                        and tc.function
                        and tc.function.arguments
                    ):
                        await self._send_stream_event({
                            "type": "tool_stream",
                            "tool_name": entry["name"],
                            "args": entry["args"],
                            "complete": False,
                        })

        if active_index is not None:
            entry = indexed[active_index]
            await self._send_stream_event({
                "type": "tool_stream",
                "tool_name": entry["name"],
                "args": entry["args"],
                "complete": True,
            })
        for idx in sorted(indexed):
            entry = indexed[idx]
            if entry["id"]:
                model.tool_calls.append(ToolCall(
                    id=entry["id"],
                    name=entry["name"],
                    arguments=entry["args"],
                ))
        return last_finish

    async def _stream_turn(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        model: ModelMessage,
        *,
        require_content: bool = True,
    ) -> None:
        """Run one chat-completion stream, retrying failed attempts.

        Unified retry mechanism for every kind of bad stream: any HTTP error,
        a premature disconnect (stream ended without a completion signal --
        neither a ``[DONE]`` marker nor a ``finish_reason``), or an empty
        completion (no content and no tool calls). A failed attempt's partial
        output -- possibly a broken thinking chain, a truncated reply, or
        tool-call fragments -- is discarded (``model`` was appended
        unpersisted by the caller) and the *identical* request is re-issued,
        so the retry is a clean replay of the same turn. Each retry broadcasts
        a ``status`` banner carrying the failure reason and a ``turn_restart``
        event, then backs off linearly starting at 5s, +3s each retry. After
        ``max_stream_retries`` attempts (config, default 10, total attempts
        including the first) the partial output is dropped and the last
        :class:`_StreamAttemptError` is re-raised for the run's error path.

        The ``finish_reason`` is the provider's single authoritative declaration of
        what the turn contains, and the output must match it (mirroring the
        OpenAI SDK, which fires done events on the finish and treats
        ``length``/``content_filter`` finishes as invalid completions):
        ``tool_calls`` requires tool calls, ``stop`` requires non-thinking
        content, and tool calls require the ``tool_calls`` seal. Tool-call
        turns routinely ship with ``content: null`` (verified against qwen3.8
        via llama.cpp: reasoning deltas + ``finish_reason="tool_calls"`` +
        ``[DONE]``, no text), so calls alone satisfy that turn. Every mismatch --
        half-baked calls, a seal without calls, stop without content,
        truncated or empty output -- discards the whole turn. Mismatches are
        retried except ``length`` truncations, which are deterministic (the
        identical request would hit the same token budget again) and fail
        immediately. ``require_content=False`` (interrupt summaries) skips
        these checks; ``_need_compact`` turns also skip them (their tool calls
        are intercepted and the loop relies on the interception results
        instead).

        ``model`` is reset in place on retry, so the caller's reference stays
        valid and history holds one clean open model message.
        """
        max_tries = max(1, int(self.config.get("max_stream_retries", 10)))
        tries = 0
        while True:
            tries += 1
            try:
                stream = await self.chat_completion(messages=messages, stream=True, tools=tools)
                last_finish = await self._drain_stream(stream, model)
                if require_content and not self._need_compact:
                    # finish_reason is the provider's single authoritative
                    # declaration of what the turn contains; the output must
                    # match it (OpenAI SDK treats it the same way: length /
                    # content_filter finishes are invalid, done events fire on
                    # the finish). Every mismatch is discarded and retried:
                    if model.tool_calls:
                        # Tool calls need the ``tool_calls`` seal -- without
                        # it the calls are half-baked (truncated arguments /
                        # protocol contradiction) and never executed.
                        if last_finish != "tool_calls":
                            if last_finish == "length":
                                # Truncated arguments are deterministic: the
                                # identical request would truncate again, so
                                # no retry -- fail fast.
                                raise _StreamAttemptError("tool calls truncated (length)", retryable=False)
                            raise _StreamAttemptError("incomplete tool calls")
                    elif last_finish == "stop":
                        # A "stop" finish declares a completed text reply: it
                        # must carry non-thinking content.
                        if not model.content:
                            raise _StreamAttemptError("stop without content")
                    elif last_finish == "tool_calls":
                        # A tool_calls seal with no calls at all: contradiction.
                        raise _StreamAttemptError("tool_calls finish without tool calls")
                    elif not model.content:
                        # length / content_filter / missing finish with nothing
                        # produced: truncated (verified against qwen3.8:
                        # thinking hit the token budget, content never
                        # started) or a plain empty completion.
                        if last_finish == "length":
                            # Truncated thinking is deterministic: the same
                            # request would hit the same token budget again.
                            # No retry -- fail fast with the reason surfaced.
                            raise _StreamAttemptError("output truncated (length)", retryable=False)
                        raise _StreamAttemptError("empty response")
                # A successful request retires any retry banner shown so far:
                # the failure is over, and the next failure starts counting
                # from 1 again (the attempt counter is per-turn anyway).
                await self._send_stream_event({"type": "status", "text": ""})
                return
            except _StreamAttemptError as e:
                self._log.warning(
                    "TURN stream try %d/%d failed: %s (think=%d content=%d calls=%d)",
                    tries, max_tries, e, len(model.think), len(model.content), len(model.tool_calls),
                )
                if not e.retryable or tries >= max_tries:
                    # Give up: either the failure is deterministic (length
                    # truncation -- a retry replays identically) or the retry
                    # budget is spent. Drop the failed attempt's partial
                    # output (never persisted) so the error state is clean and
                    # the next user message starts a fresh turn; surface the
                    # last error to the run handler.
                    self.history.remove(model)
                    self.history.close_model()
                    raise
                # Discard the partial attempt -- broken thinking, truncated
                # text, tool-call fragments -- and replay the same request.
                model.think = ""
                model.content = ""
                model.tool_calls = []
                self.history.remove(model)
                self.history.append_model(model)
                await self._send_stream_event({
                    "type": "status",
                    "text": f"⚠️ LLM request failed ({e}), retrying ({tries + 1}/{max_tries})...",
                })
                await self._send_stream_event({"type": "turn_restart"})
                # Linear backoff between attempts: 5s, then +3s each retry (5, 8, 11, ...).
                await asyncio.sleep(5 + 3 * (tries - 1))

    def _load_working_context(self) -> str:
        """Load AGENTS.md and other context from the working directory."""
        agents_md = self.working_dir / "AGENTS.md"
        if agents_md.exists():
            try:
                content = agents_md.read_text(encoding="utf-8", errors="replace")
                return f"\n\n<AGENTS.md>\n{content}\n</AGENTS.md>"
            except Exception:
                return ""
        return ""

    def _detect_git_repo(self) -> bool:
        """True when the working directory is inside a git work tree."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.working_dir), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return result.returncode == 0 and result.stdout.strip() == "true"
        except Exception:
            return False

    def _build_env_block(self) -> str:
        """Build the dynamic environment block prepended to the system prompt.

        Mirrors opencode's ``<env>`` block: gives the model its concrete
        working directory and surroundings so it works with relative paths
        instead of guessing absolute paths that land outside the workspace.
        """
        lines = [
            "<env>",
            f"  Working directory: {self.working_dir}",
            f"  Is directory a git repo: {'yes' if self._is_git_repo else 'no'}",
            f"  Platform: {platform.system().lower()}",
            "</env>",
        ]
        return "\n".join(lines)

    def _build_system_prompt(self) -> str:
        """Assemble the full system prompt from the role base plus the VLM
        block and skills listing. The VLM part depends on the session's
        current model (``enable_vl``), so model switches rebuild it."""
        prompt = self._system_base
        if self.enable_vl:
            prompt += "\n\n" + IMAGE_READ_PROMPT
        if self._skills_listing:
            prompt += "\n\n" + self._skills_listing
        return prompt

    def apply_model(self, name: str) -> None:
        """Switch the session to a predefined model.

        The change takes effect from the next LLM request (i.e. the next
        send; an in-flight run picks it up at its next completion call).
        Persisting the choice to ``session.json`` is the caller's job; here
        the agent's effective settings (endpoint, key, limits, VL flag) are
        updated so ``chat_completion`` uses the new model. The system prompt
        is rebuilt when the switch flips ``enable_vl``, and the persisted
        SystemMessage is rewritten to match (the model change already
        invalidates the provider cache, so touching the API prefix is free).
        """
        model_def = get_model(self.config, name)
        self._model_name = model_def.name
        self.model = model_def.model
        self._api_key = model_def.api_key
        self.enable_vl = model_def.enable_vl
        self.max_context_tokens = model_def.max_context
        self.max_tokens = model_def.max_tokens
        self.compaction_trigger_ratio = model_def.compaction_trigger_ratio
        self._extra_body = model_def.extra_body or {}
        self._pretend_to_be_opencode = model_def.pretend_to_be_opencode
        url = model_def.api_url.rstrip("/")
        if url != self._api_url:
            self._api_url = url
            self._http.base_url = url
        # Update opencode-style base headers when switching models.
        base_headers: dict[str, str] = {}
        if self._pretend_to_be_opencode:
            base_headers = {
                "User-Agent": "opencode/1.18.4",
                "x-session-affinity": self._session_id,
                "X-Session-Id": self._session_id,
            }
        self._http.headers.update(base_headers)
        for key in ["User-Agent", "x-session-affinity", "X-Session-Id"]:
            if not self._pretend_to_be_opencode and key in self._http.headers:
                del self._http.headers[key]
        self.system_prompt = self._build_system_prompt()
        if self.history and isinstance(self.history[0], SystemMessage):
            self.history[0].content = self.system_prompt + self.working_context

    def _ensure_system_message(self):
        """Ensure history starts with a system message.

        For new sessions or old histories without one, create it from current
        config. Once recorded, the system message is immutable in history.
        """
        if not self.history or not isinstance(self.history[0], SystemMessage):
            self.history.insert(0, SystemMessage(self.system_prompt + self.working_context))

    @property
    def _plan_mode(self) -> bool:
        """True when the primary agent is running in plan mode.

        Derived from the active permission policy rather than stored as a
        separate flag, so the permission is the single source of truth.
        """
        return isinstance(self._permission, PlanModePermission)

    def set_plan_mode(self, mode: bool) -> None:
        """Switch the primary agent between plan and build mode.

        This swaps the permission policy in place -- a mode change on a
        running agent, not a new agent definition. The notification logic
        in ``run`` notices the swap and tells the model. Subagents are fixed
        roles and ignore this.
        """
        if self._subagent_type is not None:
            return
        self._permission = PlanModePermission() if mode else BuildModePermission()

    def is_sealed(self) -> bool:
        """True for a subagent whose run has ended (view-only, no new input)."""
        return self._sealed

    @property
    def kind(self) -> str:
        """Session kind for the frontend: 'main' | 'subagent'."""
        if self._subagent_type is not None:
            return "subagent"
        return "main"

    def interrupt(self) -> None:
        """Hard-stop a running subagent's current work, then summarize.

        Immediately cancels the in-progress run -- this interrupts an LLM
        stream (the ``async for chunk in stream`` await) or a bash call right
        away, instead of waiting for the run loop to reach the next safe
        point. The run's ``CancelledError`` handler sees the interrupt flag
        and, rather than a bare cancel, runs one tool-less summary turn and
        exits. Also kills a running bash process group and unblocks a pending
        permission wait so nothing is left stuck.
        """
        self._interrupted = True
        self._permission_approved = False
        self._permission_event.set()
        self._kill_current_proc()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _register_proc(self, proc) -> None:
        """Record the bash subprocess currently running (or None when idle).

        Called from the worker thread that runs execute_tool; interrupt()
        reads this from the event loop thread. Reference assignment is atomic
        under the GIL, and Popen.kill()/poll() are safe to call cross-thread.
        """
        self._current_proc = proc

    def _kill_current_proc(self) -> None:
        """Kill a running bash process group so a cancelled/interrupted run
        does not leave an orphaned command behind.

        ``cancel()``/``stop()``/``interrupt()`` all cancel the run task, which
        raises CancelledError at the ``await asyncio.to_thread`` and orphans
        the worker thread; without killing the process group the command would
        keep running in the background. Reads ``_current_proc`` (set by the
        worker thread via ``_register_proc``).
        """
        proc = self._current_proc
        if proc is not None and proc.poll() is None:
            kill_process_group(proc)

    def _make_output_callback(self, tool_call_id: str):
        """Build a line-by-line output callback for a bash tool call.

        Runs in the worker thread that executes bash; each output line is
        forwarded to the event loop as a ``tool_output`` stream event keyed by
        ``tool_call_id`` so the frontend can append it to the matching tool.
        Lines are also accumulated in ``_tool_output_buffer`` so a cancelled
        run can salvage the partial output (the cancelled execute_tool never
        returns its result).
        """
        self._tool_output_buffer.pop(tool_call_id, None)  # fresh start
        def _on_output(text: str, stream: str) -> None:
            self._tool_output_buffer.setdefault(tool_call_id, []).append((text, stream))
            if self._loop is None:
                return
            event = {
                "type": "tool_output",
                "tool_call_id": tool_call_id,
                "stream": stream,
                "text": text,
            }
            asyncio.run_coroutine_threadsafe(
                self._send_stream_event(event), self._loop
            )
        return _on_output

    @property
    def all_additional_dirs(self) -> list[Path]:
        """Effective grants: global config dirs followed by session dirs."""
        out = list(self._global_dirs)
        for d in self._additional_dirs:
            if d not in out:
                out.append(d)
        return out

    def set_additional_dirs(self, dirs: list[str]):
        """Replace the allowed-directory grants, canonicalized and deduped.

        Grants are normalized (``~`` expanded, symlinks resolved, relative
        paths against the working dir) so the same directory can never be
        listed twice under different spellings. When this session's grants
        change, running subagents are kept in sync: they inherit the new
        grants (so they stop re-requesting what the user already allowed for
        the group) and lose the ones this session dropped.
        """
        old = self._additional_dirs
        new: list[Path] = []
        for d in dirs:
            resolved = normalize_dir(d, self.working_dir)
            if resolved not in new:
                new.append(resolved)
        self._additional_dirs = new
        if self._children and new != old:
            removed = [d for d in old if d not in new]
            for c in list(self._children):
                # Keep the child's own grants, drop what the parent removed,
                # then add the parent's current grants (union semantics).
                merged = [d for d in c._additional_dirs if d not in removed]
                for d in new:
                    if d not in merged:
                        merged.append(d)
                c.set_additional_dirs([str(d) for d in merged])

    def _add_additional_dir(self, path: str):
        """Grant one more directory, persist it, and share it with children."""
        self.set_additional_dirs([str(d) for d in self._additional_dirs] + [path])
        self._persist_additional_dirs()

    def resolve_plan_exit(self, approved: bool):
        self._plan_exit_approved = approved
        self._plan_exit_event.set()

    def resolve_permission(self, approved: bool):
        self._permission_approved = approved
        self._permission_event.set()

    def _persist_additional_dirs(self):
        """Write additional_dirs back to session.json."""
        self._update_session_json({"additional_dirs": [str(d) for d in self._additional_dirs]})

    def _persist_token_count(self):
        """Write token_count back to session.json."""
        self._update_session_json({"token_count": self._token_count})

    def _update_session_json(self, updates: dict):
        """Update fields in session.json."""
        session_json = self.session_dir / "session.json"
        if session_json.exists():
            try:
                info = json.loads(session_json.read_text())
                info.update(updates)
                session_json.write_text(json.dumps(info, indent=2))
            except Exception:
                pass

    def _maybe_auto_title(self, message: str) -> None:
        """Auto-name the session from the first user message.

        Takes the first 50 characters of the message (whitespace collapsed to
        single spaces) as the session ``name``. Skipped once a title has been
        set -- whether by this auto-naming or by a manual rename -- so re-runs
        (e.g. reverting back to the first message) and user-chosen names are
        preserved. The ``titled`` flag in session.json records this.
        """
        session_json = self.session_dir / "session.json"
        if not session_json.exists():
            return
        try:
            info = json.loads(session_json.read_text())
        except Exception:
            return
        if info.get("titled"):
            return
        title = " ".join(message.split())[:50]
        if not title:
            return
        info["name"] = title
        info["titled"] = True
        session_json.write_text(json.dumps(info, indent=2))

    async def _send_stream_event(self, event: dict):
        await self._broker.publish(event, len(self.history.raw))

    def _count_user_messages(self) -> int:
        """Count real user messages, excluding compact summaries and mode notices.

        Compact summaries are stored as :class:`UserMessage` (so the API sees
        them as user content) but are not real user turns, so they must not
        receive a ``user_seq`` - otherwise revert/edit numbering would drift.
        Mode-change notices are the same: persisted as user content for cache
        stability, but not real user turns.
        """
        return sum(
            1 for m in self.history.raw
            if isinstance(m, UserMessage)
            and not m.compact_summary
            and not m.is_mode_notification
        )

    async def _append_image_user_message(self, image: Image) -> None:
        """Insert a follow-up user message carrying an image read by a tool.

        This mirrors opencode: after a tool reads an image, the image is sent as
        a separate ``user`` message (with empty text) so the next assistant turn
        sees it as an image input. The ``<image .../>`` marker in the tool result
        serves as metadata only.
        """
        user_seq = self._count_user_messages()
        user = UserMessage("", images=[image])
        self.history.append(user)
        await self._send_stream_event({
            "type": "user",
            "id": user._id,
            "text": "",
            "images": [image.to_frontend_dict()],
            "user_seq": user_seq,
        })

    def _has_compactable_content(self) -> bool:
        """Whether there is real conversation after the last compact marker."""
        start = 0
        for i, msg in enumerate(self.history.raw):
            if isinstance(msg, CompactMarker):
                start = i + 1
        for msg in self.history.raw[start:]:
            if isinstance(msg, (UserMessage, ModelMessage)) and not (
                isinstance(msg, UserMessage)
                and (msg.compact_summary or msg.is_compact_prompt)
            ):
                return True
        return False

    def _collect_pending_steers(self) -> list[str]:
        """Real user messages that arrived after the compact prompt.

        During a compact turn the user may steer; those messages land after the
        compact prompt (and possibly after the in-flight note). They were folded
        into the note's context via ``combine_new_messages`` but are genuine
        user turns the model must still respond to on the fresh context. Left
        behind the compact marker they would be dropped from the API context,
        so the caller re-appends them after the compact summary.
        """
        raw = self.history.raw
        prompt_idx = -1
        for i in range(len(raw) - 1, -1, -1):
            m = raw[i]
            if isinstance(m, UserMessage) and m.is_compact_prompt:
                prompt_idx = i
                break
        if prompt_idx < 0:
            return []
        return [
            m.content for m in raw[prompt_idx + 1:]
            if isinstance(m, UserMessage) and not m.compact_summary and not m.is_compact_prompt
            and not m.is_mode_notification
        ]

    def _finalize_compaction(self, summary: str) -> list[str]:
        """Rebuild the context after a compaction turn.

        Drops a contentless :class:`CompactMarker` (the API-context boundary),
        a fresh :class:`SystemMessage` with the rebuilt prompt, and the summary
        wrapped in ``<compact>`` tags as a ``compact_summary`` user message.
        ``get_api_messages()`` starts just past the marker, so pre-compaction
        messages are dropped from the API context while remaining in persisted
        history.

        Returns the steering user messages that arrived during the compact turn
        so the caller can re-append them (with user events) after the summary;
        otherwise they would sit behind the marker and be lost from the API
        context.
        """
        pending_steers = self._collect_pending_steers()
        rebuilt_system = self.system_prompt + self.working_context
        self.history.append(CompactMarker())
        self.history.append(SystemMessage(rebuilt_system))
        self.history.append(UserMessage(f"<compact>\n{summary}</compact>", compact_summary=True))
        # The pre-marker <modeswitch> notice is now behind the compact marker
        # and dropped from the API context, so re-assert the current mode.
        # Syncing _last_notified_mode prevents a duplicate notice on next run.
        notif = PLAN_MODE_NOTIFICATION if self._plan_mode else BUILD_MODE_NOTIFICATION
        self.history.append(UserMessage(notif, is_mode_notification=True))
        self._last_notified_mode = self._plan_mode
        self._need_compact = False
        self._force_run = True
        self._token_count = 0
        return pending_steers

    async def run(self):
        self._task = asyncio.current_task()
        self._loop = asyncio.get_running_loop()

        # A run starting on a suspended session ends the suspension: either
        # the timer fired (the pending entry is already dropped) or the user
        # sent a message (an early wake-up). Cancel whatever suspension state
        # is left and tell the frontend the blue dot can go. The in-memory
        # flag alone is not enough after a restart: an instance restored from
        # disk starts with no flag while the TimerService still holds the
        # armed suspension, so consult the service too -- otherwise an early
        # user message would leave the stale timer armed and the session
        # would be woken twice.
        if self._sleeping_until is not None or (
            self._timer_service is not None and self._timer_service.is_sleeping(self.name)
        ):
            self._sleeping_until = None
            if self._timer_service is not None:
                self._timer_service.cancel(self.name)
            await self._send_stream_event({"type": "sleep_end", "session": self.name})

        self._ensure_system_message()
        # Guard against a dangling assistant(tool_calls) lacking results left
        # behind by a crashed/interrupted run -- the API would reject it.
        self._patch_dangling_tool_calls()

        # The current mode is conveyed to the model via a <modeswitch> user
        # message (the tool set is identical across modes for cache stability,
        # so mode awareness cannot come from which tools are listed). On the
        # first run of a session (new or restored) _last_notified_mode is None
        # and the model has not been told its mode yet, so we inject the
        # notice; afterwards we only inject on an actual change. Persisting it
        # keeps the API prefix growing monotonically so the turn stays
        # cacheable. The notice is hidden from the frontend and excluded from
        # user_seq via is_mode_notification. Scheduled agents are a fixed role
        # (no mode), so they never get a notice.
        if (
            self._last_notified_mode is None or self._plan_mode != self._last_notified_mode
        ):
            notif = PLAN_MODE_NOTIFICATION if self._plan_mode else BUILD_MODE_NOTIFICATION
            self._last_notified_mode = self._plan_mode
            self.history.append(UserMessage(notif, is_mode_notification=True))

        # The model message being streamed/mutated in the current turn
        # (None outside a turn). Held as locals so the CancelledError handler
        # can salvage or discard partial output.
        current_model: ModelMessage | None = None
        current_model_persisted = False

        try:
            while True:
                # ── continuation check ── run another turn only when there is
                # something new for the model to respond to: tool results it
                # has not seen, a user message (start/steer) it has not read,
                # or a forced turn after compaction. Otherwise the run ends.
                has_unread_user = self._count_user_messages() > self._user_read_cursor
                if not (self._pending_tool_results or has_unread_user or self._force_run):
                    break
                # A sleep_until suspension ends this run here. The flags are
                # intentionally NOT cleared: the pending tool results (with
                # the sleep placeholder) stay armed so the wake-up run
                # resumes the loop exactly where this one stopped, and the
                # wake-up message arrives as the new unread user input. A
                # steering message that landed meanwhile is an early wake-up:
                # it clears the suspension and the run continues, so the
                # user gets their response now instead of at the target time.
                if self._sleeping_until is not None:
                    if has_unread_user:
                        self._sleeping_until = None
                        if self._timer_service is not None:
                            self._timer_service.cancel(self.name)
                        await self._send_stream_event({"type": "sleep_end", "session": self.name})
                    else:
                        break
                # About to run a turn, which consumes any pending tool results
                # and forced run; clear them so a plain-text turn afterwards
                # does not spuriously continue.
                self._force_run = False
                self._pending_tool_results = False

                # Subagent step cap: prevent runaway loops.
                if self._max_steps is not None and self._step_count >= self._max_steps:
                    self._final_state = "error"
                    self._final_result = f"max_steps ({self._max_steps}) exceeded"
                    await self._send_stream_event({"type": "error", "text": self._final_result})
                    break
                messages = self.history.get_api_messages(
                    image_dir=self.session_dir / "images",
                    enable_vl=self.enable_vl,
                )
                # Record how many user messages the model is reading this turn
                # (at get_api_messages time, before the stream drains). A steer
                # arriving mid-drain lands after this cursor and drives the
                # next continuation check.
                self._user_read_cursor = self._count_user_messages()

                await self._send_stream_event({"type": "turn"})

                # The tools list is identical on every turn and across plan/build
                # mode switches (both modes advertise the full set), so the
                # request prefix stays cache-stable. Mode differences are
                # conveyed via the <modeswitch> notice and enforced by
                # permission.intercept at execution time, not by withholding
                # tool definitions. The compaction turn keeps the same tools too;
                # any tool call it makes is intercepted (see the _need_compact
                # branch below) so the tools never actually execute.
                tools = self._permission.filter_definitions(
                    enable_vl=self.enable_vl
                )
                # Begin the turn: append an empty model message and mutate it as
                # the stream drains. It is persisted only once finalized, so a
                # crash mid-turn leaves no half-written entry on disk.
                model = ModelMessage()
                self.history.append_model(model)
                current_model = model
                current_model_persisted = False
                # Drop buffered bash output from previous turns -- it is only
                # needed to salvage a cancelled in-flight command, and a new
                # turn means the previous one completed (or was salvaged).
                self._tool_output_buffer.clear()
                # One unified retry loop covers every kind of bad stream (HTTP
                # errors, premature disconnects without a completion signal):
                # the partial output is discarded and the identical request is
                # re-issued (status + turn_restart events) until
                # max_stream_retries attempts are spent.
                await self._stream_turn(messages, tools, model)

                await self._send_stream_event({
                    "type": "usage",
                    "token_count": self._token_count,
                    "max_context_tokens": self.max_context_tokens,
                })

                self._log.info(
                    "TURN result content_len=%d thinking_len=%d tool_calls=%d token_count=%d plan_mode=%s",
                    len(model.content), len(model.think), len(model.tool_calls), self._token_count, self._plan_mode,
                )
                if not model.content and not model.think and not model.tool_calls:
                    self._log.warning("TURN produced EMPTY model output (no content/thinking/tool_calls)")

                self._step_count += 1

                if model.tool_calls:
                    # Persist the model message (with tool_calls) before
                    # executing tools, so a crash between tools leaves a
                    # patchable dangling entry rather than nothing.
                    self.history.save()
                    current_model_persisted = True
                    self._pending_tool_results = True

                    for tc in model.tool_calls:
                        args = {}
                        try:
                            args = json.loads(tc.arguments)
                        except json.JSONDecodeError:
                            pass

                        tool_name = tc.name
                        await self._send_stream_event({"type": "tool_start", "tool": tool_name, "args": args, "tool_call_id": tc.id})

                        tool_result: dict[str, Any]
                        blocked = self._permission.intercept(tool_name)
                        if blocked is not None:
                            tool_result = {"result": blocked}
                        elif self._need_compact:
                            # Compaction turn: tools stay advertised (for
                            # prefix-cache stability) but must not execute. If
                            # the model calls one despite the compaction prompt,
                            # intercept it so no side effects happen; the turn
                            # is retried below until the model emits a text-only
                            # handoff summary.
                            tool_result = {"result": "Compaction in progress: tool calls are not executed during the compaction turn. Respond with the handoff summary as text only, do not call any tools."}
                        elif tool_name == "exit_plan_mode":
                            if not self._plan_mode:
                                tool_result = {"result": "exit_plan_mode is a no-op in build mode; you are already in build mode."}
                            else:
                                # Fan out to the group so the request is visible
                                # even while the user is browsing a subagent.
                                await self._broadcast_to_group({"type": "plan_exit_request", "session": self.name})
                                await self._plan_exit_event.wait()
                                self._plan_exit_event.clear()
                                if self._plan_exit_approved:
                                    self._permission = BuildModePermission()
                                    self._last_notified_mode = False
                                    tool_result = {"result": "Plan mode exited. You are now in build mode and may make file changes."}
                                else:
                                    tool_result = {"result": "User declined. Continue planning in plan mode."}
                        elif tool_name == "task":
                            tool_result = await self._run_subagents(args)
                        elif tool_name == "sleep_until":
                            tool_result = await self._run_sleep_tool(args)
                        else:
                            # Tools are synchronous (notably bash's subprocess.run
                            # blocks). Run them in a worker thread so a long bash
                            # call doesn't freeze the event loop -- otherwise the
                            # shared loop stalls SSE heartbeats and every other
                            # agent/subagent on it (issue #5).
                            on_output = self._make_output_callback(tc.id)
                            tool_result = await asyncio.to_thread(
                                execute_tool, tool_name, args, self.working_dir,
                                self.session_dir, self.all_additional_dirs,
                                self.config, self._register_proc, on_output)
                            if "image" in tool_result and self.enable_vl:
                                await self._append_image_user_message(tool_result["image"])

                        # Handle permission request from tool
                        if "permission" in tool_result:
                            perm_path = tool_result["permission"]
                            if self._subagent_type is not None and self._parent is not None:
                                # Subagent: broadcast globally (parent + all
                                # siblings) so the prompt reaches the user
                                # regardless of which session they are viewing.
                                await self._parent._broadcast_subagent_permission(
                                    self, perm_path, tool_name, tool_result["result"]
                                )
                            else:
                                # Main session: fan out to its group too.
                                await self._broadcast_to_group({
                                    "type": "permission_request",
                                    "session": self.name,
                                    "path": perm_path,
                                    "tool": tool_name,
                                    "message": tool_result["result"],
                                })
                            self._permission_event.clear()
                            await self._permission_event.wait()
                            if self._permission_approved:
                                # The frontend also calls addDir (persisting to
                                # session.json); set_additional_dirs dedupes so
                                # the approved path is never listed twice.
                                self._add_additional_dir(perm_path)
                                # Retry the tool with updated additional_dirs
                                on_output = self._make_output_callback(tc.id)
                                tool_result = await asyncio.to_thread(
                                    execute_tool, tool_name, args, self.working_dir,
                                    self.session_dir, self.all_additional_dirs,
                                    self.config, self._register_proc, on_output)
                                if "image" in tool_result and self.enable_vl:
                                    await self._append_image_user_message(tool_result["image"])
                            # else: keep original error result

                        result_event: dict[str, Any] = {"type": "tool_result", "tool": tool_name, "result": tool_result["result"], "tool_call_id": tc.id}
                        if "diff" in tool_result:
                            result_event["diff"] = tool_result["diff"]
                        await self._send_stream_event(result_event)

                        # The result goes into the ToolResults entry right
                        # after the model (created on first result, before any
                        # steering user appended meanwhile), so a steering
                        # message can never land between the tool_calls and
                        # their results.
                        self.history.insert_result(ToolResult(
                            tool_call_id=tc.id,
                            content=tool_result["result"],
                            diff=tool_result.get("diff"),
                        ))

                else:
                    self.history.save()
                    current_model_persisted = True

                # ── compaction handling after the turn ──
                if self._need_compact and model.tool_calls:
                    # The model called tools during the compaction turn despite
                    # the prompt; they were intercepted above (no side effects).
                    # Do not finalize with an empty/partial summary -- loop
                    # again so the model, now seeing the interception results,
                    # retries with a text-only handoff summary.
                    pass
                elif self._need_compact:
                    # This was the compaction turn and the model produced a
                    # text-only handoff summary. Rebuild the context (marker +
                    # fresh system + compact_summary), then re-append any
                    # steering messages that arrived during the compact turn so
                    # the model responds to them on the fresh context instead
                    # of losing them behind the marker.
                    pending_steers = self._finalize_compaction(model.content)
                    for s in pending_steers:
                        user_seq = self._count_user_messages()
                        user = UserMessage(s)
                        self.history.append(user)
                        await self._send_stream_event({"type": "user", "id": user._id, "text": s, "user_seq": user_seq})
                    self._token_covered = len(self.history.raw)
                    self._persist_token_count()
                    await self._broker.commit(len(self.history.raw))
                    await self._send_stream_event({"type": "compact"})
                elif should_compact(self):
                    # Token threshold exceeded: request a compaction turn next.
                    # The compact prompt is a marked user message -- it combines
                    # with any pending steering via combine_new_messages and the
                    # next turn asks for a handoff summary (tools advertised but
                    # intercepted). Steering that arrives during the compact
                    # turn is re-appended past the marker by _finalize_compaction
                    # so it is not lost.
                    self._need_compact = True
                    prompt = build_compact_prompt(self)
                    user_seq = self._count_user_messages()
                    user = UserMessage(prompt, is_compact_prompt=True)
                    self.history.append(user)
                    await self._send_stream_event({"type": "user", "id": user._id, "text": prompt, "user_seq": user_seq})
                elif not model.tool_calls:
                    # Plain-text final turn, nothing pending: the run completes.
                    self._final_state = "completed"
                    self._final_result = model.content

            # Loop exited normally: the run completed with no pending work.
            # Emit ``done`` only for a completed run (max_steps already
            # emitted ``error`` above).
            if self._final_state == "completed":
                await self._send_stream_event({"type": "done"})

        except asyncio.CancelledError:
            if self._interrupted:
                # A subagent interrupt hard-stops the current turn mid-flight
                # (LLM stream or bash). Clear the pending cancellation, keep
                # any partial output (e.g. half-streamed thinking) so the
                # summary turn carries it to the API, salvage partial bash
                # output, then run one summary turn before exiting cleanly --
                # so the parent's task tool gets a summary result instead of a
                # bare cancellation. (A real cancel from the parent leaves
                # _interrupted False and falls through to the hard-stop path.)
                if self._task is not None:
                    self._task.uncancel()
                # Keep partial output from an interrupted in-flight turn
                # (mid-drain, unpersisted) so the summary turn's API call
                # carries the half-streamed thinking -- a bare discard would
                # lose the model's reasoning. Only drop a truly empty
                # placeholder (nothing streamed yet). A persisted turn (mid
                # tool-execution) is already in history.
                if current_model is not None and not current_model_persisted:
                    if current_model.think or current_model.content or current_model.tool_calls:
                        self.history.save()
                    else:
                        self.history.remove(current_model)
                # Salvage partial bash output for the in-flight tool call.
                self._salvage_inflight_tool(current_model)
                self._patch_dangling_tool_calls()
                try:
                    await self._summarize_and_exit()
                except asyncio.CancelledError:
                    # Parent cancelled us mid-summary: hard-stop, no summary.
                    raise
                except Exception as e:
                    self._final_state = "error"
                    self._final_result = f"interrupt summary failed: {e}"
                    await self._send_stream_event({"type": "error", "text": self._final_result})
                return
            # Hard cancel: salvage partial output from the in-flight turn.
            self._final_state = "error"
            self._final_result = "cancelled"
            # Propagate cancellation to running subagents (hard stop, no summary).
            for c in list(self._children):
                if c._task and not c._task.done():
                    c._task.cancel()
            # Salvage partial bash output for the in-flight tool call before
            # persisting, so the model sees what the cancelled command produced
            # instead of a bare [interrupted] placeholder on the next run.
            self._salvage_inflight_tool(current_model)
            # Persist the in-flight model message if it has any content,
            # thinking, or tool_calls; otherwise drop the empty placeholder so
            # history stays clean.
            if current_model is not None:
                if current_model.content or current_model.think or current_model.tool_calls:
                    self.history.save()
                    self._log.warning(
                        "RUN cancelled content_len=%d thinking_len=%d tool_calls=%d",
                        len(current_model.content), len(current_model.think), len(current_model.tool_calls))
                else:
                    self.history.remove(current_model)
                    self._log.warning("RUN cancelled (no partial output)")
            else:
                self._log.warning("RUN cancelled")
            await self._send_stream_event({"type": "cancelled"})
            raise
        except Exception as e:
            msg = str(e)
            error_type: str | None = None
            error_code: str | None = None
            status_code: int | None = None

            if hasattr(e, "body") and isinstance(e.body, dict):
                err = e.body.get("error", {})
                msg = err.get("message", msg)
                error_type = err.get("type")
                error_code = err.get("code")

            if hasattr(e, "status_code"):
                status_code = e.status_code

            self._final_state = "error"
            self._final_result = msg
            self._log.exception("RUN error status=%s type=%s code=%s msg=%s",
                                status_code, error_type, error_code, msg)
            await self._send_stream_event({
                "type": "error",
                "text": msg,
                "status_code": status_code,
                "error_type": error_type,
                "error_code": error_code,
            })
        finally:
            # A subagent's run has ended for any reason -> it is now sealed
            # (view-only, no new input).
            if self._subagent_type is not None:
                self._sealed = True
            # The run is over regardless of how it ended; clear the running
            # flag (a safety net -- done/cancelled/error already set it) and
            # drop the task reference so is_running() is accurate.
            self.history.close_model()
            self._broker.set_running(False)
            self._task = None

    async def steer(self, message: str):
        """Append a steering user message mid-run.

        The message goes straight into history (no queue): the run loop is
        alive, so its next continuation check picks it up and runs another turn
        so the model can respond. It lands after the in-flight assistant turn,
        never splitting tool_calls from their results.
        """
        user_seq = self._count_user_messages()
        user = UserMessage(message)
        self.history.append(user)
        await self._send_stream_event({"type": "user", "id": user._id, "text": message, "user_seq": user_seq})

    def add_user_message(self, message: str, images: list[Image] | None = None) -> UserMessage:
        user = UserMessage(message, images=images)
        self.history.append(user)
        return user

    def is_running(self) -> bool:
        return self._broker.is_running

    async def start(self, message: str, images: list[Image] | None = None) -> None:
        """Begin a run independently of any HTTP request lifecycle.

        Closing a browser only drops SSE subscribers; the agent keeps running
        because the task created here is not tied to any request. The user
        message is emitted as a stream event so every subscriber (and any
        reconnecting client) renders it consistently.
        """
        user_seq = self._count_user_messages()
        # Auto-title the session from the first user message (first 50 chars).
        # Subagents carry a description instead and are never auto-titled.
        if user_seq == 0 and self._subagent_type is None:
            self._maybe_auto_title(message)
        user = self.add_user_message(message, images=images)
        self._broker.set_running(True)
        await self._send_stream_event({
            "type": "user",
            "id": user._id,
            "text": message,
            "images": [img.to_frontend_dict() for img in (images or [])] or None,
            "user_seq": user_seq,
        })
        self._task = asyncio.create_task(self.run())

    async def attach_subscriber(self) -> tuple[asyncio.Queue, dict]:
        """Subscribe a client: replay the current run and snapshot history."""
        q, snapshot = await self._broker.attach(
            self.history.raw,
            self._token_count,
            self.max_context_tokens,
            self._plan_mode,
            [str(d) for d in self._additional_dirs],
        )
        # Expand the committed typed messages into the flat role-based dict list
        # the frontend expects (a ModelMessage unfolds into its assistant
        # dict; a ToolResults into its tool-result dicts).
        snapshot["history"] = [
            d for m in snapshot["history"] for d in m.to_frontend_dicts()
        ]
        snapshot["is_subagent"] = self._subagent_type is not None
        snapshot["kind"] = self.kind
        snapshot["sealed"] = self._sealed
        snapshot["subagent_type"] = self._subagent_type
        snapshot["description"] = self._description
        snapshot["enable_vl"] = self.enable_vl
        # Global config grants are session-independent; the sidebar shows
        # them in gray with no remove button.
        snapshot["global_dirs"] = [str(d) for d in self._global_dirs]
        return q, snapshot

    def detach_subscriber(self, q: asyncio.Queue) -> None:
        self._broker.detach(q)

    async def aclose(self) -> None:
        """Release async resources (the httpx client).

        Web mode keeps agents alive for the process lifetime, so this is only
        needed by the CLI, which owns a single short-lived agent.
        """
        await self._http.aclose()

    def cancel(self):
        self._kill_current_proc()
        if self._task and not self._task.done():
            self._task.cancel()
        # Propagate cancellation to running subagents (hard stop, no summary).
        for c in list(self._children):
            c.cancel()

    async def stop(self) -> None:
        """Cancel an in-progress run and wait for it to fully finish."""
        self._kill_current_proc()
        task = self._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        for c in list(self._children):
            await c.stop()
        self._task = None
        self._broker.set_running(False)

    # ── subagent spawning ────────────────────────────────────────────────

    def _create_child(self, subagent_type: str, description: str, prompt: str) -> Agent:
        """Build a child Agent for one subtask (does not start it)."""
        child_name = uuid.uuid4().hex
        child_dir = self.session_dir.parent / child_name
        child_dir.mkdir(parents=True, exist_ok=True)
        info = {
            "name": child_name,
            "working_dir": str(self.working_dir),
            "parent_session": self.name,
            "subagent_type": subagent_type,
            "description": description,
            "depth": self._depth + 1,
            "additional_dirs": [str(d) for d in self._additional_dirs],
            "model": self._model_name,
            "created_at": time.time(),
        }
        (child_dir / "session.json").write_text(json.dumps(info, indent=2))
        child = Agent(
            name=child_name,
            working_dir=str(self.working_dir),
            session_dir=child_dir,
            config=self.config,
            subagent_type=subagent_type,
            description=description,
            registry=self._registry,
            parent=self,
            depth=self._depth + 1,
            max_steps=int(self.config.get("subagent_max_steps", 100)),
            model_name=self._model_name,
        )
        child.set_additional_dirs([str(d) for d in self._additional_dirs])
        child.add_user_message(prompt)
        child._initial_prompt = prompt
        if self._registry is not None:
            self._registry[child_name] = child
        return child

    async def _run_as_subagent(self) -> None:
        """Mark running, emit the initial prompt as a user event, then run.

        Each subagent runs as its own asyncio task so it can be steered or
        interrupted/cancelled independently.
        """
        self._broker.set_running(True)
        await self._send_stream_event({"type": "user", "text": self._initial_prompt, "user_seq": 0})
        await self.run()

    # ── timers (sleep_until) ───────────────────────────────────────────────

    async def _run_sleep_tool(self, args: dict) -> dict[str, Any]:
        """Suspend this session until a target time (virtual sleep_until tool).

        Registers the suspension with the TimerService (persisted to the
        session's session.json) and arms ``_sleeping_until`` -- the run loop
        breaks on it once this turn's tool calls are done. The returned
        placeholder is recorded as the tool result, so on wake-up the model
        sees what it asked for (and when) next to the wake-up message.
        """
        if self._timer_service is None:
            return {"result": "Error: sleep_until is only available for server sessions."}
        wake_at, err = parse_sleep_until(str(args.get("until") or ""))
        if err is not None:
            return {"result": err}
        self._timer_service.register(self.name, wake_at)
        self._sleeping_until = wake_at
        await self._send_stream_event({
            "type": "sleep_start",
            "session": self.name,
            "until": wake_at,
        })
        return {"result": sleep_placeholder(wake_at)}

    async def _run_subagents(self, args: dict) -> dict[str, Any]:
        """Spawn one or more subagents in parallel, gather their results."""
        specs = args.get("tasks") or []
        if not isinstance(specs, list) or not specs:
            return {"result": "Error: task tool requires a non-empty 'tasks' array."}

        children: list[Agent] = []
        errors: list[str] = []
        for spec in specs:
            if not isinstance(spec, dict):
                errors.append("[invalid task spec]")
                continue
            stype = spec.get("subagent_type")
            desc = spec.get("description", "subagent")
            prompt = spec.get("prompt", "")
            if stype not in ("explore", "general"):
                errors.append(f"[{desc}] invalid subagent_type: {stype}")
                continue
            if self._plan_mode and stype == "general":
                errors.append(f"[{desc}] plan mode can only spawn explore (read-only) subagents")
                continue
            if self._depth >= 1:
                errors.append(f"[{desc}] subagent nesting limit reached")
                continue
            children.append(self._create_child(stype, desc, prompt))

        await self._send_stream_event({
            "type": "subagents",
            "parent": self.name,
            "children": [
                {"session": c.name, "type": c._subagent_type, "description": c._description, "state": "running"}
                for c in children
            ],
        })

        async def _run_and_announce(c: Agent) -> None:
            """Run a child subagent, then emit its terminal state immediately.

            Emitting per-child (rather than after ``gather`` returns) lets the
            task bubble update each subagent row as it finishes, instead of all
            at once when the last one completes.
            """
            await c._run_as_subagent()
            await self._send_stream_event(
                {"type": "subagent_state", "session": c.name, "state": c._final_state}
            )

        self._children = children
        tasks = [asyncio.create_task(_run_and_announce(c)) for c in children]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._children = []

        parts: list[str] = []
        for c in children:
            state = c._final_state
            text = c._final_result
            parts.append(
                f'<subagent session="{c.name}" type="{c._subagent_type}" '
                f'description="{c._description}" state="{state}">\n'
                f"<result>{text}</result>\n</subagent>"
            )

        body = "\n".join(parts)
        if errors:
            body = "\n".join(errors) + "\n" + body
        return {"result": f"<task_result>\n{body}\n</task_result>"}

    def _format_partial_bash_output(self, tool_call_id: str, args: dict) -> str | None:
        """Reconstruct the partial output streamed so far for a bash call.

        Mirrors BashTool's output shape (stdout, then a ``[stderr]`` section)
        but from the ``on_output`` buffer accumulated in the worker thread,
        since the cancelled ``execute_tool`` never returns its result. Returns
        None when nothing was streamed.
        """
        buf = self._tool_output_buffer.get(tool_call_id)
        if not buf:
            return None
        snapshot = list(buf)  # copy: reader threads may still append
        out_lines = [text for text, src in snapshot if src == "stdout"]
        err_lines = [text for text, src in snapshot if src == "stderr"]
        output = "\n".join(out_lines)
        if err_lines:
            if output:
                output += "\n"
            output += "[stderr]\n" + "\n".join(err_lines)
        if not output:
            return None
        max_lines = args.get("max_output_lines", 100)
        max_chars = args.get("max_output_chars", 10000)
        return truncate_output(output, max_lines, max_chars)

    def _salvage_inflight_tool(self, model: ModelMessage | None) -> None:
        """Fill the in-flight bash tool's result with partial output on cancel.

        When a run is cancelled mid-tool-execution, the executing tool call has
        no result yet. For bash, salvage the output streamed so far (via
        ``on_output``) and insert a note that the user cancelled, so the model
        sees what the command produced instead of a bare ``[interrupted]``
        placeholder. Only the first unanswered tool call (the one actually
        executing) is considered; later, not-yet-started calls are left for
        ``_patch_dangling_tool_calls``.
        """
        if model is None or not model.tool_calls:
            return
        entry = self.history.tool_results_of(model)
        answered = {tr.tool_call_id for tr in entry.results} if entry else set()
        for tc in model.tool_calls:
            if tc.id and tc.id not in answered:
                # The first unanswered tool call is the one that was executing.
                if tc.name == "bash":
                    try:
                        args = json.loads(tc.arguments)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    partial = self._format_partial_bash_output(tc.id, args)
                    note = "\n[command cancelled by user; output above is partial]"
                    content = (partial + note) if partial else "[command cancelled by user]"
                    self.history.insert_result(
                        ToolResult(tool_call_id=tc.id, content=content),
                        after=model,
                    )
                    self._tool_output_buffer.pop(tc.id, None)
                break

    def _patch_dangling_tool_calls(self) -> None:
        """Fill placeholder tool results for tool_calls lacking a result.

        An interrupt or crash can leave a :class:`ModelMessage` with
        ``tool_calls`` but not all results in its :class:`ToolResults` entry.
        OpenAI-compatible APIs reject ``tool_calls`` without matching results,
        which would break the next turn. Scan for the last such model (it may
        not be the trailing entry -- steering messages can sit after it) and
        insert a placeholder result for any unanswered ``tool_call_id``.
        """
        raw = self.history.raw
        last_model: ModelMessage | None = None
        for m in raw:
            if isinstance(m, ModelMessage) and m.tool_calls:
                last_model = m
        if last_model is None:
            return
        entry = self.history.tool_results_of(last_model)
        answered = {tr.tool_call_id for tr in entry.results} if entry else set()
        pending = [
            tc for tc in last_model.tool_calls
            if tc.id and tc.id not in answered
        ]
        if pending:
            self.history.insert_results(
                [ToolResult(tool_call_id=tc.id, content="[interrupted]") for tc in pending],
                after=last_model,
            )

    async def _summarize_and_exit(self) -> None:
        """Produce a final summary turn after an interrupt, then exit.

        No further tool calls are allowed; one tool-less LLM call yields the
        summary that becomes this subagent's result.
        """
        summary_prompt = "你被中断了。请简明总结你目前的发现/进展，然后停止。"
        user = UserMessage(summary_prompt)
        self.history.append(user)
        await self._send_stream_event({"type": "user", "id": user._id, "text": summary_prompt, "user_seq": self._count_user_messages() - 1})
        await self._send_stream_event({"type": "turn"})
        messages = self.history.get_api_messages(
            image_dir=self.session_dir / "images",
            enable_vl=self.enable_vl,
        )
        model = ModelMessage()
        self.history.append_model(model)
        # Same unified retry loop as regular turns: flaky providers get a
        # second chance instead of failing the interrupted subagent outright.
        # require_content=False keeps the [no summary produced] fallback.
        await self._stream_turn(messages, None, model, require_content=False)
        if not model.content:
            model.content = "[no summary produced]"
        self.history.save()
        self.history.close_model()
        self._final_state = "interrupted"
        self._final_result = model.content
        await self._send_stream_event({"type": "interrupted"})

    async def _broadcast_to_group(self, event: dict) -> None:
        """Send an event to this agent's own stream and, for the main session,
        to all its running children. Used for the main session's own approval
        requests (permission / plan-exit) so the prompt is visible no matter
        which session the user is currently viewing."""
        await self._send_stream_event(event)
        if self._subagent_type is None:
            for c in list(self._children):
                await c._send_stream_event(event)

    async def _broadcast_subagent_permission(self, child: Agent, path: str, tool: str, message: str) -> None:
        """Fan a subagent's permission request to the parent and all running
        sibling subagents, so the prompt is visible no matter which session
        the user is currently viewing."""
        event = {
            "type": "subagent_permission_request",
            "child_session": child.name,
            "child_type": child._subagent_type,
            "child_description": child._description,
            "path": path,
            "tool": tool,
            "message": message,
        }
        await self._send_stream_event(event)
        for c in list(self._children):
            await c._send_stream_event(event)

    async def revert(self, message_id: str, message: str) -> str:
        """Edit a previously sent user message and rerun from there.

        Two cases:

        * The message has already been read by the model (its user_seq is
          below the read cursor): stop the run, truncate history at that
          message, then start a fresh run with the edited text.
        * The message has not been read yet (a steering message still pending
          in history): swap its text in place without interrupting the run.

        Returns ``"rerun"`` or ``"queued"`` so the client knows whether to
        reconnect (rerun rebuilds from the truncated history) or just apply a
        local text update (queued).
        """
        target = self._turns.find_user(message_id)
        seq = self.history.user_seq_of(target)
        if seq < self._user_read_cursor or not self.is_running():
            # Stop first: the cancelled run's salvage logic may still reference
            # messages that are about to be truncated away.
            if self.is_running():
                await self.stop()
            self.history.truncate_at(message_id)
            # Truncation removed the messages the cursor counted past; align it
            # to the truncated history so start()'s new message reads as unread
            # and the run loop actually turns. Otherwise (k+1) > old_cursor is
            # false and the run exits immediately without calling the model.
            self._user_read_cursor = self._count_user_messages()
            await self._broker.commit(len(self.history.raw))
            await self.start(message)
            return "rerun"

        # Not yet read and a run is live (steering message pending): edit in place.
        target.content = message
        self.history.save()
        await self._send_stream_event({"type": "user_edit", "message_id": message_id, "text": message})
        return "queued"

    async def compact_now(self) -> None:
        """Manually trigger context compaction (the ``/compact`` command).

        Compacts regardless of the token threshold. The compact prompt is
        appended as a normal user message and the run loop handles the rest:
        the next turn asks for a handoff summary (tools advertised but
        intercepted), then the context is rebuilt and the model continues on
        the fresh context. If there is no real conversation to compact (e.g.
        right after a previous
        compact), a status notice is sent instead.
        """
        self._broker.set_running(True)

        if not self._has_compactable_content():
            await self._send_stream_event({"type": "status", "text": "nothing to compact"})
            await self._send_stream_event({
                "type": "usage",
                "token_count": self._token_count,
                "max_context_tokens": self.max_context_tokens,
            })
            await self._send_stream_event({"type": "done"})
            self._broker.set_running(False)
            self._task = None
            return

        self._need_compact = True
        prompt = build_compact_prompt(self)
        user_seq = self._count_user_messages()
        user = UserMessage(prompt, is_compact_prompt=True)
        self.history.append(user)
        await self._send_stream_event({"type": "user", "id": user._id, "text": prompt, "user_seq": user_seq})
        self._task = asyncio.create_task(self.run())

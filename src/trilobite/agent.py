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

from src.trilobite.broker import StreamBroker
from src.trilobite.compaction import should_compact, build_compact_prompt
from src.trilobite.config import DEFAULT_MAX_CONTEXT_TOKENS, DEFAULT_MAX_TOKENS
from src.trilobite.history import History
from src.trilobite.prompts import SYSTEM_PROMPT, subagent_system_prompt
from src.trilobite.permission import AgentPermission, BuildModePermission, ExploreSubagentPermission, GeneralSubagentPermission, PlanModePermission
from src.trilobite.tools.bash import kill_process_group
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

PLAN_MODE_NOTIFICATION = (
    "Your operational mode has changed from build to plan.\n"
    "You are now in read-only mode.\n"
    "You are not permitted to make file changes. Focus on exploring, analyzing, and planning."
)

BUILD_MODE_NOTIFICATION = (
    "Your operational mode has changed from plan to build.\n"
    "You are no longer in read-only mode.\n"
    "You are permitted to make file changes, run shell commands, and utilize your arsenal of tools as needed."
)


# ── httpx-based OpenAI-compatible streaming chat completions ────────────────

async def _chat_completion_stream(
    http: httpx.AsyncClient,
    api_key: str,
    body: dict,
    log: logging.Logger | None = None,
):
    """Stream SSE chunks from an OpenAI-compatible chat/completions endpoint."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
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
                        for t in d.get("tool_calls", []):
                            tc_list.append(_ToolCall(
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
            if not done_seen:
                if log:
                    log.warning("STREAM ended WITHOUT [DONE] after %d chunks, finish_reasons=%s",
                                chunk_count, finish_reasons)
    except Exception as e:
        if log:
            log.exception("STREAM error: %r", e)
        raise


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
    ):
        self.name = name
        self.working_dir = Path(working_dir).resolve()
        self.session_dir = session_dir
        self.config = config
        self._session_id = session_id or _generate_session_id()
        self._api_url = config["api_url"].rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._api_url,
            headers={
                "User-Agent": "opencode/1.18.4",
                "x-session-affinity": self._session_id,
                "X-Session-Id": self._session_id,
            },
            timeout=httpx.Timeout(600, connect=10),
        )
        self.model = config["model"]
        self._log = logging.getLogger(f"trilobite.agent.{name}")
        if not self._log.handlers:
            _fh = logging.FileHandler(self.session_dir / "agent.log", encoding="utf-8")
            _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._log.addHandler(_fh)
        self._log.setLevel(config.get("log_level", "WARNING"))
        self._log.propagate = False
        self.reasoning_effort = config.get("reasoning_effort", "max")
        self.max_context_tokens = int(config.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS))
        self.max_tokens = int(config.get("max_tokens", DEFAULT_MAX_TOKENS))
        self.compaction_trigger_ratio = float(config.get("compaction_trigger_ratio", 0.7))
        self.system_prompt = SYSTEM_PROMPT
        # Subagents override the system prompt with the role prefix + guidance
        # and use a fixed role permission (never plan/build mode).
        self._subagent_type: str | None = subagent_type
        self._description: str = description or ""
        if subagent_type == "explore":
            self.system_prompt = subagent_system_prompt("explore")
        elif subagent_type == "general":
            self.system_prompt = subagent_system_prompt("general")
        # Prepend a dynamic environment block (working dir, git, platform)
        # so the model knows where it is and prefers relative paths over
        # guessed absolute paths that drift outside the workspace.
        self._is_git_repo = self._detect_git_repo()
        self.system_prompt = self._build_env_block() + "\n\n" + self.system_prompt
        self.working_context = self._load_working_context()
        self.history = History(session_dir / "history.json")
        self._broker = StreamBroker(len(self.history.raw))
        self._token_count: int = 0
        self._token_covered: int = 0
        self._task: asyncio.Task | None = None
        self._steering: asyncio.Event = asyncio.Event()
        self._steer_messages: list[str] = []
        if subagent_type == "explore":
            self._permission: AgentPermission = ExploreSubagentPermission()
        elif subagent_type == "general":
            self._permission: AgentPermission = GeneralSubagentPermission()
        else:
            self._permission: AgentPermission = BuildModePermission()
        self._last_notified_mode: bool | None = None
        self._additional_dirs: list[Path] = []
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
        self._initial_prompt: str = ""
        self._final_state: str = "completed"
        self._final_result: str = ""

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
        if stream:
            body["stream_options"] = {"include_usage": True}
            if self.reasoning_effort:
                body["reasoning_effort"] = self.reasoning_effort
                body["thinking"] = {"type": "enabled"}
            return _chat_completion_stream(self._http, self.config["api_key"], body, log=self._log)
        else:
            headers = {
                "Authorization": f"Bearer {self.config['api_key']}",
                "Content-Type": "application/json",
            }
            resp = await self._http.post("/chat/completions", json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()

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

    def _ensure_system_message(self):
        """Ensure history starts with a system message.

        For new sessions or old histories without one, create it from current
        config. Once recorded, the system message is immutable in history.
        """
        if not self.history or self.history[0].get("role") != "system":
            self.history.insert(0, {"role": "system", "content": self.system_prompt + self.working_context})

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
        proc = self._current_proc
        if proc is not None and proc.poll() is None:
            kill_process_group(proc)
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _register_proc(self, proc) -> None:
        """Record the bash subprocess currently running (or None when idle).

        Called from the worker thread that runs execute_tool; interrupt()
        reads this from the event loop thread. Reference assignment is atomic
        under the GIL, and Popen.kill()/poll() are safe to call cross-thread.
        """
        self._current_proc = proc

    def _make_output_callback(self, tool_call_id: str):
        """Build a line-by-line output callback for a bash tool call.

        Runs in the worker thread that executes bash; each output line is
        forwarded to the event loop as a ``tool_output`` stream event keyed by
        ``tool_call_id`` so the frontend can append it to the matching tool.
        """
        def _on_output(text: str, stream: str) -> None:
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

    def set_additional_dirs(self, dirs: list[str]):
        self._additional_dirs = [Path(d).resolve() for d in dirs]

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
        import json
        session_json = self.session_dir / "session.json"
        if session_json.exists():
            try:
                info = json.loads(session_json.read_text())
                info.update(updates)
                session_json.write_text(json.dumps(info, indent=2))
            except Exception:
                pass

    async def _send_stream_event(self, event: dict):
        await self._broker.publish(event, len(self.history.raw))

    def _count_user_messages(self) -> int:
        """Count real user messages, excluding compact summaries.

        Compact summaries are stored as ``role: user`` (so the API sees them
        as user content) but are not real user turns, so they must not
        receive a ``user_seq`` - otherwise revert/edit numbering would drift.
        """
        return sum(
            1 for m in self.history.raw
            if m.get("role") == "user" and not m.get("compact_summary")
        )

    def _has_compactable_content(self) -> bool:
        """Whether there is real conversation after the last compact marker."""
        start = 0
        for i, msg in enumerate(self.history.raw):
            if msg.get("compact_marker"):
                start = i + 1
        for msg in self.history.raw[start:]:
            if msg.get("compact_summary") or msg.get("role") == "system":
                continue
            return True
        return False

    async def _compact_turn(self) -> None:
        """Run one compaction turn.

        Appends the compact instruction as a user message, streams the model's
        handoff summary (visible like a normal turn), then inserts a compact
        marker (rebuilt system prompt) followed by the summary wrapped in
        ``<compact>`` tags as a user message.

        Resulting history tail::

            ..., {user: compact prompt}, {assistant: summary},
            {system: rebuilt prompt, compact_marker}, {user: <compact>summary</compact>},
            ...new conversation...

        ``get_api_messages()`` starts from the marker, so pre-compaction
        messages are dropped from the API context while remaining in the
        persisted (frontend) history.
        """
        prompt = build_compact_prompt(self)
        user_seq = self._count_user_messages()
        self.history.append({"role": "user", "content": prompt})
        await self._send_stream_event({"type": "user", "text": prompt, "user_seq": user_seq})

        messages = self.history.get_api_messages()
        await self._send_stream_event({"type": "turn"})
        stream = await self.chat_completion(messages=messages, stream=True, tools=None)

        content_parts: list[str] = []
        thinking_parts: list[str] = []
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue
            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                thinking_parts.append(delta.reasoning_content)
                await self._send_stream_event({"type": "thinking", "text": delta.reasoning_content})
            if delta.content:
                content_parts.append(delta.content)
                await self._send_stream_event({"type": "text", "text": delta.content})

        summary = "".join(content_parts) or "[compaction produced no output]"

        assistant_msg: dict = {"role": "assistant", "content": summary}
        if thinking_parts:
            assistant_msg["reasoning_content"] = "".join(thinking_parts)
        self.history.append(assistant_msg)

        rebuilt_system = self.system_prompt + self.working_context
        self.history.append({"role": "system", "content": rebuilt_system, "compact_marker": True})
        self.history.append({"role": "user", "content": f"<compact>\n{summary}\n</compact>", "compact_summary": True})
        await self._send_stream_event({"type": "compact"})

        self._token_count = 0
        self._token_covered = len(self.history.raw)
        self._persist_token_count()

    async def run(self):
        self._task = asyncio.current_task()
        self._loop = asyncio.get_running_loop()

        content_parts: list[str] = []
        thinking_parts: list[str] = []

        self._ensure_system_message()

        # Check for mode change once per run (when user sends a message).
        # Injected into messages list, not stored in history.
        mode_notification: str | None = None
        if self._last_notified_mode is None:
            self._last_notified_mode = self._plan_mode
        elif self._plan_mode != self._last_notified_mode:
            mode_notification = PLAN_MODE_NOTIFICATION if self._plan_mode else BUILD_MODE_NOTIFICATION
            self._last_notified_mode = self._plan_mode

        try:
            while True:
                # Subagent step cap: prevent runaway loops.
                if self._max_steps is not None and self._step_count >= self._max_steps:
                    self._final_state = "error"
                    self._final_result = f"max_steps ({self._max_steps}) exceeded"
                    await self._send_stream_event({"type": "error", "text": self._final_result})
                    break
                if should_compact(self):
                    await self._compact_turn()
                    await self._broker.commit(len(self.history.raw))
                    continue
                messages = self.history.get_api_messages()
                if mode_notification:
                    messages.insert(1, {"role": "user", "content": mode_notification})
                    mode_notification = None

                await self._send_stream_event({"type": "turn"})

                stream = await self.chat_completion(
                    messages=messages,
                    stream=True,
                    tools=self._permission.filter_definitions(),
                )

                content_parts.clear()
                thinking_parts.clear()
                tool_calls: list[dict] = []
                content_parts: list[str] = []
                thinking_parts: list[str] = []
                current_tool_id = ""
                current_tool_name = ""
                current_tool_args = ""

                async for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None

                    # Capture real token usage from stream
                    if chunk.usage:
                        self._token_count = chunk.usage.total_tokens
                        self._token_covered = len(self.history)
                        self._persist_token_count()

                    if delta is None:
                        continue

                    if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                        thinking_parts.append(delta.reasoning_content)
                        await self._send_stream_event({"type": "thinking", "text": delta.reasoning_content})

                    if delta.content:
                        content_parts.append(delta.content)
                        await self._send_stream_event({"type": "text", "text": delta.content})

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            if tc.id:
                                if current_tool_id:
                                    await self._send_stream_event({
                                        "type": "tool_stream",
                                        "tool_name": current_tool_name,
                                        "args": current_tool_args,
                                        "complete": True,
                                    })
                                    tool_calls.append({
                                        "id": current_tool_id,
                                        "type": "function",
                                        "function": {"name": current_tool_name, "arguments": current_tool_args},
                                    })
                                current_tool_id = tc.id
                                current_tool_name = tc.function.name if tc.function else ""
                                current_tool_args = ""
                                if current_tool_name:
                                    await self._send_stream_event({
                                        "type": "tool_stream",
                                        "tool_name": current_tool_name,
                                        "args": "",
                                        "complete": False,
                                    })
                            if tc.function:
                                if tc.function.name:
                                    current_tool_name = tc.function.name
                                    await self._send_stream_event({
                                        "type": "tool_stream",
                                        "tool_name": current_tool_name,
                                        "args": current_tool_args,
                                        "complete": False,
                                    })
                                if tc.function.arguments:
                                    current_tool_args += tc.function.arguments
                                    await self._send_stream_event({
                                        "type": "tool_stream",
                                        "tool_name": current_tool_name,
                                        "args": current_tool_args,
                                        "complete": False,
                                    })

                if current_tool_id:
                    await self._send_stream_event({
                        "type": "tool_stream",
                        "tool_name": current_tool_name,
                        "args": current_tool_args,
                        "complete": True,
                    })
                    tool_calls.append({
                        "id": current_tool_id,
                        "type": "function",
                        "function": {"name": current_tool_name, "arguments": current_tool_args},
                    })

                await self._send_stream_event({
                    "type": "usage",
                    "token_count": self._token_count,
                    "max_context_tokens": self.max_context_tokens,
                })

                content = "".join(content_parts)
                thinking = "".join(thinking_parts)

                self._log.info(
                    "TURN result content_len=%d thinking_len=%d tool_calls=%d token_count=%d plan_mode=%s",
                    len(content), len(thinking), len(tool_calls), self._token_count, self._plan_mode,
                )
                if not content and not thinking and not tool_calls:
                    self._log.warning("TURN produced EMPTY assistant output (no content/thinking/tool_calls)")

                self._step_count += 1

                if tool_calls:
                    assistant_msg: dict = {"role": "assistant", "tool_calls": tool_calls}
                    if content:
                        assistant_msg["content"] = content
                    if thinking:
                        assistant_msg["reasoning_content"] = thinking
                    self.history.append(assistant_msg)

                    for tc in tool_calls:
                        args = {}
                        try:
                            args = json.loads(tc["function"]["arguments"])
                        except json.JSONDecodeError:
                            pass

                        tool_name = tc["function"]["name"]
                        await self._send_stream_event({"type": "tool_start", "tool": tool_name, "args": args, "tool_call_id": tc["id"]})

                        tool_result: dict[str, Any]
                        blocked = self._permission.intercept(tool_name)
                        if blocked is not None:
                            tool_result = {"result": blocked}
                        elif tool_name == "exit_plan_mode":
                            if not self._plan_mode:
                                tool_result = {"result": "Not in plan mode."}
                            else:
                                await self._send_stream_event({"type": "plan_exit_request"})
                                await self._plan_exit_event.wait()
                                self._plan_exit_event.clear()
                                if self._plan_exit_approved:
                                    self._permission = BuildModePermission()
                                    self._last_notified_mode = False
                                    tool_result = {"result": "Plan mode exited. All tools are now available."}
                                else:
                                    tool_result = {"result": "User declined. Continue planning in plan mode."}
                        elif tool_name == "task":
                            tool_result = await self._run_subagents(args)
                        else:
                            # Tools are synchronous (notably bash's subprocess.run
                            # blocks). Run them in a worker thread so a long bash
                            # call doesn't freeze the event loop -- otherwise the
                            # shared loop stalls SSE heartbeats and every other
                            # agent/subagent on it (issue #5).
                            on_output = self._make_output_callback(tc["id"])
                            tool_result = await asyncio.to_thread(
                                execute_tool, tool_name, args, self.working_dir,
                                self.session_dir, self._additional_dirs,
                                self._register_proc, on_output)

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
                                await self._send_stream_event({
                                    "type": "permission_request",
                                    "path": perm_path,
                                    "tool": tool_name,
                                    "message": tool_result["result"],
                                })
                            self._permission_event.clear()
                            await self._permission_event.wait()
                            if self._permission_approved:
                                self._additional_dirs.append(Path(perm_path).resolve())
                                self._persist_additional_dirs()
                                # Retry the tool with updated additional_dirs
                                on_output = self._make_output_callback(tc["id"])
                                tool_result = await asyncio.to_thread(
                                    execute_tool, tool_name, args, self.working_dir,
                                    self.session_dir, self._additional_dirs,
                                    self._register_proc, on_output)
                            # else: keep original error result

                        result_event: dict[str, Any] = {"type": "tool_result", "tool": tool_name, "result": tool_result["result"], "tool_call_id": tc["id"]}
                        if "diff" in tool_result:
                            result_event["diff"] = tool_result["diff"]
                        await self._send_stream_event(result_event)

                        history_msg: dict[str, Any] = {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": tool_result["result"],
                        }
                        if "diff" in tool_result:
                            history_msg["diff"] = tool_result["diff"]
                        self.history.append(history_msg)

                    # Check for steering between tool calls
                    self._check_steer()
                else:
                    assistant_final: dict = {"role": "assistant", "content": content or ""}
                    if thinking:
                        assistant_final["reasoning_content"] = thinking
                    self.history.append(assistant_final)
                    self._final_state = "completed"
                    self._final_result = content or ""
                    await self._send_stream_event({"type": "done"})
                    break

        except asyncio.CancelledError:
            if self._interrupted:
                # A subagent interrupt hard-stops the current turn mid-flight
                # (LLM stream or bash). Discard the partial output, clear the
                # pending cancellation, and run one summary turn before exiting
                # cleanly -- so the parent's task tool gets a summary result
                # instead of a bare cancellation. (A real cancel from the
                # parent leaves _interrupted False and falls through to the
                # hard-stop path below.)
                if self._task is not None:
                    self._task.uncancel()
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
            content = "".join(content_parts)
            thinking = "".join(thinking_parts)
            self._final_state = "error"
            self._final_result = "cancelled"
            # Propagate cancellation to running subagents (hard stop, no summary).
            for c in list(self._children):
                if c._task and not c._task.done():
                    c._task.cancel()
            self._log.warning("RUN cancelled content_len=%d thinking_len=%d", len(content), len(thinking))
            if content or thinking:
                msg: dict = {"role": "assistant", "content": content or ""}
                if thinking:
                    msg["reasoning_content"] = thinking
                self.history.append(msg)
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
            # flag (a safety net — done/cancelled/error already set it) and
            # drop the task reference so is_running() is accurate.
            self._broker.set_running(False)
            self._task = None

    def _check_steer(self) -> bool:
        """Check if steering messages were queued and add them to history. Returns True if steered."""
        if not self._steer_messages:
            return False
        messages = [{"role": "user", "content": m} for m in self._steer_messages]
        self._steer_messages.clear()
        self._steering.clear()
        self.history.extend(messages)
        return True

    async def steer(self, message: str):
        user_seq = self._count_user_messages() + len(self._steer_messages)
        await self._send_stream_event({"type": "user", "text": message, "user_seq": user_seq})
        self._steer_messages.append(message)
        self._steering.set()

    def add_user_message(self, message: str):
        self.history.append({"role": "user", "content": message})

    def is_running(self) -> bool:
        return self._broker.is_running

    async def start(self, message: str) -> None:
        """Begin a run independently of any HTTP request lifecycle.

        Closing a browser only drops SSE subscribers; the agent keeps running
        because the task created here is not tied to any request. The user
        message is emitted as a stream event so every subscriber (and any
        reconnecting client) renders it consistently.
        """
        user_seq = self._count_user_messages()
        self.add_user_message(message)
        self._broker.set_running(True)
        await self._send_stream_event({"type": "user", "text": message, "user_seq": user_seq})
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
        snapshot["is_subagent"] = self._subagent_type is not None
        snapshot["sealed"] = self._sealed
        snapshot["subagent_type"] = self._subagent_type
        snapshot["description"] = self._description
        return q, snapshot

    def detach_subscriber(self, q: asyncio.Queue) -> None:
        self._broker.detach(q)

    def cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()
        # Propagate cancellation to running subagents (hard stop, no summary).
        for c in list(self._children):
            c.cancel()

    async def stop(self) -> None:
        """Cancel an in-progress run and wait for it to fully finish."""
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

    def _patch_dangling_tool_calls(self) -> None:
        """Fill placeholder tool results for tool_calls lacking a result.

        An interrupt cancels a turn mid-flight. If the cancellation lands
        after the assistant message (with ``tool_calls``) was appended but
        before every tool result landed, history is left inconsistent:
        OpenAI-compatible APIs reject ``tool_calls`` that are not followed by
        a matching ``tool`` result, which would break the summary turn. Scan
        the trailing assistant message and append a placeholder result for
        any unanswered ``tool_call_id``.
        """
        raw = self.history.raw
        if not raw:
            return
        last = raw[-1]
        if last.get("role") != "assistant" or not last.get("tool_calls"):
            return
        answered = {m.get("tool_call_id") for m in raw if m.get("role") == "tool"}
        for tc in last["tool_calls"]:
            tc_id = tc.get("id")
            if tc_id and tc_id not in answered:
                self.history.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": "[interrupted]",
                })

    async def _summarize_and_exit(self) -> None:
        """Produce a final summary turn after an interrupt, then exit.

        No further tool calls are allowed; one tool-less LLM call yields the
        summary that becomes this subagent's result.
        """
        summary_prompt = "你被中断了。请简明总结你目前的发现/进展，然后停止。"
        self.history.append({"role": "user", "content": summary_prompt})
        await self._send_stream_event({"type": "user", "text": summary_prompt, "user_seq": self._count_user_messages() - 1})
        await self._send_stream_event({"type": "turn"})
        messages = self.history.get_api_messages()
        stream = await self.chat_completion(messages=messages, stream=True, tools=None)
        parts: list[str] = []
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue
            if getattr(delta, "content", None):
                parts.append(delta.content)
                await self._send_stream_event({"type": "text", "text": delta.content})
        summary = "".join(parts) or "[no summary produced]"
        self.history.append({"role": "assistant", "content": summary})
        self._final_state = "interrupted"
        self._final_result = summary
        await self._send_stream_event({"type": "interrupted"})

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

    async def revert(self, user_seq: int, message: str) -> str:
        """Edit a previously sent user message and rerun from there.

        Two cases:

        * The message is already in history (the model has seen it): stop the
          run, truncate history to just before that message, then start a fresh
          run with the edited text.
        * The message is still queued as a steering message the model has not
          read yet: replace it in place without interrupting the run.

        Returns ``"rerun"`` or ``"queued"`` so the client knows whether to
        reconnect (rerun rebuilds from the truncated history) or just apply a
        local text update (queued).
        """
        history_users = self._count_user_messages()
        if user_seq < history_users:
            if self.is_running():
                await self.stop()
            target = -1
            count = 0
            for i, msg in enumerate(self.history.raw):
                if msg.get("role") == "user":
                    if count == user_seq:
                        target = i
                        break
                    count += 1
            self.history.truncate(target)
            await self._broker.commit(target)
            await self.start(message)
            return "rerun"

        # Still queued, not yet read by the model: swap the text in place.
        k = user_seq - history_users
        if not (0 <= k < len(self._steer_messages)):
            raise ValueError("user message not found")
        self._steer_messages[k] = message
        await self._send_stream_event({"type": "user_edit", "user_seq": user_seq, "text": message})
        return "queued"

    async def compact_now(self) -> None:
        """Manually trigger context compaction (the ``/compact`` command).

        Compacts regardless of the token threshold. The compact turn is
        streamed like normal output. If there is no real conversation to
        compact (e.g. right after a previous compact), a status notice is sent
        instead. After compaction, if a steering message arrived mid-compact,
        a normal run is kicked off to consume it; otherwise the run ends.
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

        await self._compact_turn()
        await self._broker.commit(len(self.history.raw))
        await self._send_stream_event({
            "type": "usage",
            "token_count": self._token_count,
            "max_context_tokens": self.max_context_tokens,
        })
        if self._steer_messages:
            self._task = asyncio.create_task(self.run())
        else:
            await self._send_stream_event({"type": "done"})
            self._broker.set_running(False)
            self._task = None

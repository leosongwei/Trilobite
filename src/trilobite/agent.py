from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from src.trilobite.compaction import compact_if_needed
from src.trilobite.config import DEFAULT_MAX_CONTEXT_TOKENS, load_system_prompt
from src.trilobite.history import History
from src.trilobite.tool_call import execute_tool, get_tool_definitions

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
):
    """Stream SSE chunks from an OpenAI-compatible chat/completions endpoint."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    async with http.stream(
        "POST",
        "/chat/completions",
        json=body,
        headers=headers,
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
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
                    choices.append(_Choice(delta=_Delta(
                        content=d.get("content"),
                        reasoning_content=d.get("reasoning_content"),
                        tool_calls=tc_list if tc_list else None,
                    )))
                usage_raw = data.get("usage")
                usage = _Usage(total_tokens=usage_raw.get("total_tokens", 0)) if usage_raw else None
                yield _StreamChunk(choices=choices, usage=usage)
            except json.JSONDecodeError:
                continue


class Agent:
    def __init__(self, name: str, working_dir: str, session_dir: Path, config: dict[str, str], session_id: str | None = None):
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
        self.reasoning_effort = config.get("reasoning_effort", "max")
        self.max_context_tokens = int(config.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS))
        self.compaction_trigger_ratio = float(config.get("compaction_trigger_ratio", 0.7))
        self.system_prompt = load_system_prompt()
        self.working_context = self._load_working_context()
        self.history = History(session_dir / "history.json")
        self._compacted_summary: str | None = None
        self._token_count: int = 0
        self._token_covered: int = 0
        self._task: asyncio.Task | None = None
        self._stream_queue: asyncio.Queue[dict] | None = None
        self._steering: asyncio.Event = asyncio.Event()
        self._steer_messages: list[str] = []
        self._plan_mode: bool = False
        self._last_notified_mode: bool | None = None
        self._additional_dirs: list[Path] = []
        self._plan_exit_event: asyncio.Event = asyncio.Event()
        self._plan_exit_approved: bool = False
        self._permission_event: asyncio.Event = asyncio.Event()
        self._permission_approved: bool = False
        self._permission_path: str = ""

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
        if stream:
            body["stream_options"] = {"include_usage": True}
            if self.reasoning_effort:
                body["reasoning_effort"] = self.reasoning_effort
                body["thinking"] = {"type": "enabled"}
            return _chat_completion_stream(self._http, self.config["api_key"], body)
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

    def _ensure_system_message(self):
        """Ensure history starts with a system message.

        For new sessions or old histories without one, create it from current
        config. Once recorded, the system message is immutable in history.
        """
        if not self.history or self.history[0].get("role") != "system":
            self.history.insert(0, {"role": "system", "content": self.system_prompt + self.working_context})

    def set_plan_mode(self, mode: bool):
        self._plan_mode = mode

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
        if self._stream_queue is not None:
            await self._stream_queue.put(event)

    async def run(self, stream_queue: asyncio.Queue[dict]):
        self._stream_queue = stream_queue
        self._task = asyncio.current_task()

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
                if await compact_if_needed(self):
                    continue
                messages = self.history.get_api_messages()
                if mode_notification:
                    messages.insert(1, {"role": "user", "content": mode_notification})
                    mode_notification = None

                await self._send_stream_event({"type": "turn"})

                stream = await self.chat_completion(
                    messages=messages,
                    stream=True,
                    tools=get_tool_definitions(),
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
                        await self._send_stream_event({"type": "tool_start", "tool": tool_name, "args": args})

                        tool_result: dict[str, Any]
                        if tool_name == "exit_plan_mode":
                            if not self._plan_mode:
                                tool_result = {"result": "Not in plan mode."}
                            else:
                                await self._send_stream_event({"type": "plan_exit_request"})
                                await self._plan_exit_event.wait()
                                self._plan_exit_event.clear()
                                if self._plan_exit_approved:
                                    self._plan_mode = False
                                    self._last_notified_mode = False
                                    tool_result = {"result": "Plan mode exited. All tools are now available."}
                                else:
                                    tool_result = {"result": "User declined. Continue planning in plan mode."}
                        elif self._plan_mode and tool_name == "write":
                            tool_result = {"result": "Error: write tool is not available in plan mode. Call exit_plan_mode to request switching to build mode."}
                        else:
                            tool_result = execute_tool(tool_name, args, self.working_dir, self.session_dir, self._additional_dirs)

                        # Handle permission request from tool
                        if "permission" in tool_result:
                            perm_path = tool_result["permission"]
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
                                tool_result = execute_tool(tool_name, args, self.working_dir, self.session_dir, self._additional_dirs)
                            # else: keep original error result

                        result_event: dict[str, Any] = {"type": "tool_result", "tool": tool_name, "result": tool_result["result"]}
                        if "diff_prev" in tool_result:
                            result_event["diff_prev"] = tool_result["diff_prev"]
                            result_event["diff_current"] = tool_result["diff_current"]
                        await self._send_stream_event(result_event)

                        history_msg: dict[str, Any] = {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": tool_result["result"],
                        }
                        if "diff_prev" in tool_result:
                            history_msg["diff_prev"] = tool_result["diff_prev"]
                            history_msg["diff_current"] = tool_result["diff_current"]
                        self.history.append(history_msg)

                    # Check for steering between tool calls
                    if self._check_steer():
                        await self._send_stream_event({"type": "status", "text": "steered - processing new input..."})
                else:
                    assistant_final: dict = {"role": "assistant", "content": content or ""}
                    if thinking:
                        assistant_final["reasoning_content"] = thinking
                    self.history.append(assistant_final)
                    await self._send_stream_event({"type": "done"})
                    break

        except asyncio.CancelledError:
            content = "".join(content_parts)
            thinking = "".join(thinking_parts)
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

            await self._send_stream_event({
                "type": "error",
                "text": msg,
                "status_code": status_code,
                "error_type": error_type,
                "error_code": error_code,
            })

    def _check_steer(self) -> bool:
        """Check if steering messages were queued and add them to history. Returns True if steered."""
        if not self._steer_messages:
            return False
        messages = [{"role": "user", "content": m} for m in self._steer_messages]
        self._steer_messages.clear()
        self._steering.clear()
        self.history.extend(messages)
        return True

    def steer(self, message: str):
        self._steer_messages.append(message)
        self._steering.set()

    def add_user_message(self, message: str):
        self.history.append({"role": "user", "content": message})

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()

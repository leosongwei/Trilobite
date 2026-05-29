from __future__ import annotations

import asyncio
import json
from pathlib import Path

from openai import AsyncOpenAI

from src.myagent.compaction import COMPACTION_INSTRUCTION, find_compact_boundary
from src.myagent.config import load_system_prompt
from src.myagent.tokens import estimate_tokens_for_messages, estimate_tokens_for_tools
from src.myagent.tool_call import execute_tool, get_tool_definitions


class Agent:
    def __init__(self, name: str, working_dir: str, session_dir: Path, config: dict[str, str]):
        self.name = name
        self.working_dir = Path(working_dir).resolve()
        self.session_dir = session_dir
        self.config = config
        self.client = AsyncOpenAI(api_key=config["api_key"], base_url=config["api_url"])
        self.model = config["model"]
        self.reasoning_effort = config.get("reasoning_effort", "max")
        self.max_context_tokens = int(config.get("max_context_tokens", 64000))
        self.compaction_trigger_ratio = float(config.get("compaction_trigger_ratio", 0.7))
        self.system_prompt = load_system_prompt()
        self.working_context = self._load_working_context()
        self.history: list[dict] = []
        self._compacted_summary: str | None = None
        self._token_count: int = 0
        self._token_covered: int = 0
        self._task: asyncio.Task | None = None
        self._stream_queue: asyncio.Queue[dict] | None = None
        self._steering: asyncio.Event = asyncio.Event()
        self._steer_message: str | None = None

        self.history_path = session_dir / "history.json"
        self._load_history()

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

    def _load_history(self):
        if self.history_path.exists():
            try:
                self.history = json.loads(self.history_path.read_text())
            except Exception:
                self.history = []

    def _save_history(self):
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(json.dumps(self.history, indent=2, ensure_ascii=False))

    def _estimated_tokens(self) -> int:
        """Estimate total tokens including system prompt, working context, tools, and pending messages."""
        tools = get_tool_definitions()
        pending = self.history[self._token_covered :]
        return (
            estimate_tokens_for_messages([{"role": "system", "content": self.system_prompt + self.working_context}])
            + self._token_count
            + estimate_tokens_for_messages(pending)
            + estimate_tokens_for_tools(tools)
        )

    def _should_compact(self) -> bool:
        threshold = int(self.max_context_tokens * self.compaction_trigger_ratio)
        return self._estimated_tokens() >= threshold

    async def _do_compact(self):
        boundary = find_compact_boundary(self.history)
        if boundary < 1:
            return

        compact_messages = self.history[: boundary + 1]
        recent_messages = self.history[boundary + 1 :]

        system_with_context = self.system_prompt + self.working_context
        messages = [
            {"role": "system", "content": system_with_context},
            *compact_messages,
            {"role": "user", "content": COMPACTION_INSTRUCTION},
        ]

        await self._send_stream_event({"type": "status", "text": "compacting context..."})

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False,
            )
            summary = response.choices[0].message.content or ""
        except Exception:
            summary = "[compaction failed — older context dropped]"

        self._compacted_summary = summary
        summary_msg = f"[Context summary]\n{summary}"
        if self.working_context:
            summary_msg += f"\n\n[Working context — project rules]\n{self.working_context.strip()}"
        self.history = [
            {"role": "user", "content": summary_msg},
            {"role": "assistant", "content": "Understood. Continuing with the task."},
            *recent_messages,
        ]
        self._token_count = 0
        self._token_covered = 2  # the summary exchange
        self._save_history()

    async def _send_stream_event(self, event: dict):
        if self._stream_queue is not None:
            await self._stream_queue.put(event)

    async def run(self, stream_queue: asyncio.Queue[dict]):
        self._stream_queue = stream_queue
        self._task = asyncio.current_task()

        content_parts: list[str] = []
        thinking_parts: list[str] = []

        try:
            while True:
                if self._should_compact():
                    await self._do_compact()
                messages = [{"role": "system", "content": self.system_prompt + self.working_context}, *self.history]

                await self._send_stream_event({"type": "turn"})

                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=get_tool_definitions(),
                    stream=True,
                    reasoning_effort=self.reasoning_effort,
                    extra_body={"thinking": {"type": "enabled"}},
                    stream_options={"include_usage": True},
                )

                content_parts.clear()
                thinking_parts.clear()
                tool_calls: list[dict] = []
                content_parts: list[str] = []
                thinking_parts: list[str] = []
                current_tool_id = ""
                current_tool_name = ""
                current_tool_args = ""

                async for chunk in response:
                    delta = chunk.choices[0].delta if chunk.choices else None

                    # Capture real token usage from stream
                    if chunk.usage:
                        self._token_count = chunk.usage.total_tokens
                        self._token_covered = len(self.history)

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

                        result = execute_tool(tool_name, args, self.working_dir, self.session_dir)
                        await self._send_stream_event({"type": "tool_result", "tool": tool_name, "result": result})

                        self.history.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        })

                    self._save_history()

                    # Check for steering between tool calls
                    if self._check_steer():
                        await self._send_stream_event({"type": "status", "text": "steered - processing new input..."})
                else:
                    if content:
                        assistant_final: dict = {"role": "assistant", "content": content}
                        if thinking:
                            assistant_final["reasoning_content"] = thinking
                        self.history.append(assistant_final)
                    self._save_history()
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
                self._save_history()
            await self._send_stream_event({"type": "cancelled"})
            raise
        except Exception as e:
            msg = str(e)
            if hasattr(e, "body") and isinstance(e.body, dict):
                err = e.body.get("error", {})
                msg = err.get("message", msg)
            await self._send_stream_event({"type": "error", "text": msg})

    def _check_steer(self) -> bool:
        """Check if a steering message was queued and add it to history. Returns True if steered."""
        if self._steer_message is not None:
            msg = self._steer_message
            self._steer_message = None
            self._steering.clear()
            self.history.append({"role": "user", "content": msg})
            self._save_history()
            return True
        return False

    def steer(self, message: str):
        self._steer_message = message
        self._steering.set()

    def add_user_message(self, message: str):
        self.history.append({"role": "user", "content": message})
        self._save_history()

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from openai import AsyncOpenAI

from .config import load_system_prompt
from .tools import execute_tool

MAX_HISTORY_TOKENS = 64000
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read content from a file in the working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Path to the file relative to working directory."},
                    "limit_lines": {"type": "integer", "default": 50, "description": "Max lines to return."},
                    "start_line": {"type": "integer", "default": 0, "description": "Line number to start from (0-indexed)."},
                    "limit_chars": {"type": "integer", "default": 10000, "description": "Max characters to return."},
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write to a file by replacing old_str with new_str. old_str must be unique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Path to the file relative to working directory."},
                    "old_str": {"type": "string", "description": "The exact string to replace."},
                    "new_str": {"type": "string", "description": "The string to replace it with."},
                },
                "required": ["filename", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command in the working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_read",
            "description": "List all current todos.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": "Add new todos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {"type": "array", "items": {"type": "string"}, "description": "List of tasks to add."},
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_done",
            "description": "Mark todos as done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {"type": "array", "items": {"type": "string"}, "description": "List of tasks to mark as done."},
                },
                "required": ["tasks"],
            },
        },
    },
]


class Agent:
    def __init__(self, name: str, working_dir: str, session_dir: Path, config: dict[str, str]):
        self.name = name
        self.working_dir = Path(working_dir).resolve()
        self.session_dir = session_dir
        self.config = config
        self.client = AsyncOpenAI(api_key=config["api_key"], base_url=config["api_url"])
        self.model = config["model"]
        self.system_prompt = load_system_prompt()
        self.history: list[dict] = []
        self._task: asyncio.Task | None = None
        self._stream_queue: asyncio.Queue[dict] | None = None
        self._steering: asyncio.Event = asyncio.Event()
        self._steer_message: str | None = None

        self.history_path = session_dir / "history.json"
        self._load_history()

    def _load_history(self):
        if self.history_path.exists():
            try:
                self.history = json.loads(self.history_path.read_text())
            except Exception:
                self.history = []

    def _save_history(self):
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(json.dumps(self.history, indent=2, ensure_ascii=False))

    def _compact_history(self):
        while len(self.history) > 4:
            estimated = sum(len(json.dumps(m)) for m in self.history)
            if estimated <= MAX_HISTORY_TOKENS * 4:
                break
            self.history.pop(0)
        self._save_history()

    async def _send_stream_event(self, event: dict):
        if self._stream_queue is not None:
            await self._stream_queue.put(event)

    async def run(self, stream_queue: asyncio.Queue[dict]):
        self._stream_queue = stream_queue
        self._task = asyncio.current_task()

        try:
            while True:
                self._compact_history()
                messages = [{"role": "system", "content": self.system_prompt}, *self.history]

                await self._send_stream_event({"type": "status", "text": "thinking..."})

                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    stream=True,
                )

                tool_calls: list[dict] = []
                content_parts: list[str] = []
                current_tool_id = ""
                current_tool_name = ""
                current_tool_args = ""

                async for chunk in response:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta is None:
                        continue

                    if delta.content:
                        content_parts.append(delta.content)
                        await self._send_stream_event({"type": "text", "text": delta.content})

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            if tc.id:
                                if current_tool_id:
                                    tool_calls.append({
                                        "id": current_tool_id,
                                        "type": "function",
                                        "function": {"name": current_tool_name, "arguments": current_tool_args},
                                    })
                                current_tool_id = tc.id
                                current_tool_name = tc.function.name if tc.function else ""
                                current_tool_args = ""
                            if tc.function:
                                if tc.function.name:
                                    current_tool_name = tc.function.name
                                if tc.function.arguments:
                                    current_tool_args += tc.function.arguments

                if current_tool_id:
                    tool_calls.append({
                        "id": current_tool_id,
                        "type": "function",
                        "function": {"name": current_tool_name, "arguments": current_tool_args},
                    })

                content = "".join(content_parts)

                if tool_calls:
                    assistant_msg: dict = {"role": "assistant", "tool_calls": tool_calls}
                    if content:
                        assistant_msg["content"] = content
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
                        self.history.append({"role": "assistant", "content": content})
                    self._save_history()
                    await self._send_stream_event({"type": "done"})
                    break

        except asyncio.CancelledError:
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

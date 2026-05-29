import json
from pathlib import Path
from typing import Any

from src.myagent.tools.tool import Tool


class TodoReadTool(Tool):
    name = "todo_read"
    description = "List all current todos."
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, working_dir: Path, session_dir: Path, **kwargs: Any) -> str:
        todo_path = session_dir / "todos.json"
        if not todo_path.exists():
            return "TODO:\n(no todos)"
        todos: list[dict] = json.loads(todo_path.read_text())
        lines = ["TODO:"]
        for item in todos:
            done_mark = " - DONE" if item.get("done") else ""
            lines.append(f"* {item['task']}{done_mark}")
        return "\n".join(lines)


class TodoWriteTool(Tool):
    name = "todo_write"
    description = "Add new todos."
    parameters = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of tasks to add.",
            },
        },
        "required": ["tasks"],
    }

    def execute(self, working_dir: Path, session_dir: Path, tasks: list[str] | None = None, **kwargs: Any) -> str:
        todo_path = session_dir / "todos.json"
        existing = []
        if todo_path.exists():
            existing = json.loads(todo_path.read_text())
        for task in (tasks or []):
            existing.append({"task": task, "done": False})
        todo_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
        reader = TodoReadTool()
        return reader.execute(working_dir, session_dir)


class TodoDoneTool(Tool):
    name = "todo_done"
    description = "Mark todos as done."
    parameters = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of tasks to mark as done.",
            },
        },
        "required": ["tasks"],
    }

    def execute(self, working_dir: Path, session_dir: Path, tasks: list[str] | None = None, **kwargs: Any) -> str:
        todo_path = session_dir / "todos.json"
        if not todo_path.exists():
            return "TODO:\n(no todos)"
        existing = json.loads(todo_path.read_text())
        for item in existing:
            if item["task"] in (tasks or []):
                item["done"] = True
        todo_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
        reader = TodoReadTool()
        return reader.execute(working_dir, session_dir)

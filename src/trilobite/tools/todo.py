import json
from pathlib import Path
from typing import Any

from src.trilobite.tools.tool import Tool


class TodoListTool(Tool):
    name = "TodoList"
    description = """Manage the task list for your current coding session.

*When to use:*
- For multi-step tasks, plan your work by adding todos
- When you begin working on a task, mark it in_progress
- When you complete a task, mark it done
- When you finish a major phase, update the full list

*How to use:*
- Omit `todos` to read the current list (no changes)
- Pass `todos: []` to clear the list
- Pass `todos: [...]` to replace the entire list with a new set of items

*Rules:*
- At most ONE item in_progress at any time. Finish or pause current work before starting a new task.
- Each `title` should be short and actionable (a single sentence).
- When unsure of current state, read first (omit `todos`) before deciding what to update.
- Do NOT re-call this tool when nothing has meaningfully changed.
- If no available tool can move any task forward, tell the user where you are stuck instead of repeatedly re-ordering todos."""

    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short, actionable title for the todo."},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "done"],
                            "description": "Current status of the todo.",
                        },
                    },
                    "required": ["title", "status"],
                },
                "description": "Full list of todos. Omit to read current list. Pass [] to clear.",
            },
        },
        "required": [],
    }

    _TODO_FILE = "todos.json"

    def execute(self, working_dir: Path, session_dir: Path, todos: list[dict] | None = None, **kwargs: Any) -> str:
        todo_path = session_dir / self._TODO_FILE

        if todos is None:
            return self._render(todo_path)

        sanitized = self._sanitize(todos)
        todo_path.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False))

        if not sanitized:
            return "Todo list cleared."
        return "Todo list updated.\n" + self._render(todo_path)

    def _sanitize(self, todos: list[dict]) -> list[dict]:
        valid_statuses = {"pending", "in_progress", "done"}
        result = []
        for t in todos:
            title = str(t.get("title", "")).strip()
            status = str(t.get("status", "pending"))
            if not title:
                continue
            if status not in valid_statuses:
                status = "pending"
            result.append({"title": title, "status": status})
        return result

    @staticmethod
    def _render(todo_path: Path) -> str:
        if not todo_path.exists():
            return "Current todo list:\n(no todos)"

        todos: list[dict] = json.loads(todo_path.read_text())
        if not todos:
            return "Current todo list:\n(no todos)"

        icons = {"pending": "○", "in_progress": "◐", "done": "●"}
        lines = ["Current todo list:"]
        for item in todos:
            icon = icons.get(item.get("status", "pending"), "○")
            lines.append(f"  {icon} [{item.get('status', 'pending')}] {item['title']}")
        return "\n".join(lines)

import json
import subprocess
from pathlib import Path
from typing import Any


def _read_file(working_dir: Path, filename: str, limit_lines: int = 50, start_line: int = 0, limit_chars: int = 10000) -> str:
    filepath = (working_dir / filename).resolve()
    if not filepath.is_relative_to(working_dir):
        return "Error: Access denied - file is outside working directory"
    if not filepath.exists():
        return f"Error: File not found: {filename}"
    if filepath.is_dir():
        return f"Error: {filename} is a directory"
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading file: {e}"

    lines = content.splitlines()
    sliced = lines[start_line : start_line + limit_lines]
    text = "\n".join(sliced)
    if len(text) > limit_chars:
        text = text[:limit_chars] + "\n... [truncated]"
    if start_line + limit_lines < len(lines):
        text += f"\n... [file has {len(lines)} lines total]"
    return text


def _write_file(working_dir: Path, filename: str, old_str: str, new_str: str) -> str:
    filepath = (working_dir / filename).resolve()
    if not filepath.is_relative_to(working_dir):
        return "Error: Access denied - file is outside working directory"

    existed = filepath.exists()
    is_dir = existed and filepath.is_dir()

    if old_str == "":
        if is_dir:
            return f"Error: {filename} is a directory"
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(new_str, encoding="utf-8")
        action = "Created" if not existed else "Written"
        return f"{action}: {filename}"

    if not existed:
        return f"Error: File not found: {filename} (use empty old_str to create)"
    if is_dir:
        return f"Error: {filename} is a directory"

    content = filepath.read_text(encoding="utf-8")
    count = content.count(old_str)
    if count == 0:
        return "Error: old_str not found in file"
    if count > 1:
        return f"Error: old_str found {count} times in file - must be unique"

    new_content = content.replace(old_str, new_str, 1)
    filepath.write_text(new_content, encoding="utf-8")
    return f"File updated: {filename}"


def _bash(working_dir: Path, command: str) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=working_dir,
        )
        output = result.stdout
        if result.stderr:
            output += "\n[stderr]\n" + result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Command timed out (30s)"
    except Exception as e:
        return f"Error: {e}"


def _todo_read(session_dir: Path) -> str:
    todo_path = session_dir / "todos.json"
    if not todo_path.exists():
        return "TODO:\n(no todos)"
    todos: list[dict] = json.loads(todo_path.read_text())
    lines = ["TODO:"]
    for item in todos:
        done_mark = " - DONE" if item.get("done") else ""
        lines.append(f"* {item['task']}{done_mark}")
    return "\n".join(lines)


def _todo_write(session_dir: Path, tasks: list[str]) -> str:
    todo_path = session_dir / "todos.json"
    existing = []
    if todo_path.exists():
        existing = json.loads(todo_path.read_text())
    for task in tasks:
        existing.append({"task": task, "done": False})
    todo_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    return _todo_read(session_dir)


def _todo_done(session_dir: Path, tasks: list[str]) -> str:
    todo_path = session_dir / "todos.json"
    if not todo_path.exists():
        return "TODO:\n(no todos)"
    existing = json.loads(todo_path.read_text())
    for item in existing:
        if item["task"] in tasks:
            item["done"] = True
    todo_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    return _todo_read(session_dir)


def execute_tool(tool_name: str, arguments: dict[str, Any], working_dir: Path, session_dir: Path) -> str:
    match tool_name:
        case "read":
            return _read_file(
                working_dir,
                str(arguments.get("filename", "")),
                int(arguments.get("limit_lines", 50)),
                int(arguments.get("start_line", 0)),
                int(arguments.get("limit_chars", 10000)),
            )
        case "write":
            return _write_file(
                working_dir,
                str(arguments.get("filename", "")),
                str(arguments.get("old_str", "")),
                str(arguments.get("new_str", "")),
            )
        case "bash":
            return _bash(working_dir, str(arguments.get("command", "")))
        case "todo_read":
            return _todo_read(session_dir)
        case "todo_write":
            return _todo_write(session_dir, arguments.get("tasks", []))
        case "todo_done":
            return _todo_done(session_dir, arguments.get("tasks", []))
        case _:
            return f"Unknown tool: {tool_name}"

from pathlib import Path
from typing import Any

from . import Tool


class ReadTool(Tool):
    name = "read"
    description = "Read content from a file in the working directory."
    parameters = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Path to the file relative to working directory.",
            },
            "limit_lines": {
                "type": "integer",
                "default": 50,
                "description": "Max lines to return.",
            },
            "start_line": {
                "type": "integer",
                "default": 0,
                "description": "Line number to start from (0-indexed).",
            },
            "limit_chars": {
                "type": "integer",
                "default": 10000,
                "description": "Max characters to return.",
            },
        },
        "required": ["filename"],
    }

    def execute(
        self,
        working_dir: Path,
        session_dir: Path,
        filename: str = "",
        limit_lines: int = 50,
        start_line: int = 0,
        limit_chars: int = 10000,
        **kwargs: Any,
    ) -> str:
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

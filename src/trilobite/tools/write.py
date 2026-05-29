from pathlib import Path
from typing import Any

from src.trilobite.tools.tool import Tool


class WriteTool(Tool):
    name = "write"
    description = "Write to a file. If old_str is empty, create/overwrite the file. Otherwise replace old_str with new_str (old_str must be unique in the file)."
    parameters = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Path to the file relative to working directory.",
            },
            "old_str": {
                "type": "string",
                "description": "The exact string to replace. Use empty string (\"\") to create/overwrite the file.",
            },
            "new_str": {
                "type": "string",
                "description": "The string to replace it with, or the entire file content if old_str is empty.",
            },
        },
        "required": ["filename", "old_str", "new_str"],
    }

    def execute(
        self,
        working_dir: Path,
        session_dir: Path,
        filename: str = "",
        old_str: str = "",
        new_str: str = "",
        **kwargs: Any,
    ) -> str:
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

from pathlib import Path
from typing import Any

from src.trilobite.file_access import resolve_file_path
from src.trilobite.tools.tool import Tool

_CONTEXT_LINES = 6


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
        additional_dirs: list[Path] | None = None,
        filename: str = "",
        old_str: str = "",
        new_str: str = "",
        **kwargs: Any,
    ) -> str | dict[str, Any]:
        filepath, error, perm_path = resolve_file_path(filename, working_dir, additional_dirs)
        if perm_path:
            return {"result": error, "permission": perm_path}
        if error:
            return error

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

        diff_prev, diff_current = _build_context_diff(content, new_content, old_str)
        return {
            "result": f"File updated: {filename}",
            "diff_prev": diff_prev,
            "diff_current": diff_current,
        }


def _build_context_diff(old_content: str, new_content: str, old_str: str) -> tuple[str, str]:
    """Extract the changed region with surrounding context lines.

    Since new_content == old_content.replace(old_str, new_str, 1), the prefix
    before the change is identical.  We find a context window in old_content
    and shift the end position by the size difference to get the same window
    in new_content.
    """
    old_len = len(old_content)
    pos = old_content.index(old_str)
    old_end = pos + len(old_str)
    size_delta = len(new_content) - len(old_content)

    # Find ctx_start: go back _CONTEXT_LINES newlines from pos
    ctx_start = pos
    for _ in range(_CONTEXT_LINES):
        nl = old_content.rfind("\n", 0, ctx_start)
        if nl == -1:
            ctx_start = 0
            break
        ctx_start = nl
    if ctx_start > 0:
        ctx_start += 1

    # Find ctx_end: go forward _CONTEXT_LINES newlines from old_end
    ctx_end = old_end
    for _ in range(_CONTEXT_LINES):
        nl = old_content.find("\n", ctx_end)
        if nl == -1:
            ctx_end = old_len
            break
        ctx_end = nl + 1
    # Include trailing newline of last context line
    if ctx_end < old_len:
        nl = old_content.find("\n", ctx_end)
        ctx_end = nl + 1 if nl != -1 else old_len

    diff_prev = old_content[ctx_start:ctx_end]
    # Same window in new_content, adjusted for size difference
    new_ctx_end = min(ctx_end + size_delta, len(new_content))
    diff_current = new_content[ctx_start:new_ctx_end]

    return diff_prev, diff_current

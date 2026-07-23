from pathlib import Path
from typing import Any, Literal

from src.trilobite.file_access import resolve_file_path
from src.trilobite.tools.tool import Tool

_CONTEXT_LINES = 6

LineEndingStyle = Literal["lf", "crlf", "mixed"]


class EditTool(Tool):
    name = "edit"
    description = (
        "Exact string replacement in an existing file. old_string must be "
        "unique unless replace_all is set; it must not be empty (use write to "
        "create or overwrite a file)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Path to the file relative to working directory.",
            },
            "old_string": {
                "type": "string",
                "description": "The exact text to replace, copied verbatim from the read output.",
            },
            "new_string": {
                "type": "string",
                "description": "The text to replace it with.",
            },
            "replace_all": {
                "type": "boolean",
                "default": False,
                "description": "Replace every occurrence of old_string. Default false.",
            },
        },
        "required": ["filename", "old_string", "new_string"],
    }

    def execute(
        self,
        working_dir: Path,
        session_dir: Path,
        additional_dirs: list[Path] | None = None,
        filename: str = "",
        old_string: str = "",
        new_string: str = "",
        replace_all: bool = False,
        **kwargs: Any,
    ) -> str | dict[str, Any]:
        filepath, error, perm_path = resolve_file_path(filename, working_dir, additional_dirs)
        if perm_path:
            return {"result": error, "permission": perm_path}
        if error:
            return error

        if not filepath.exists():
            return f"Error: File not found: {filename} (use the write tool to create it)"
        if filepath.is_dir():
            return f"Error: {filename} is a directory"
        if old_string == "":
            return "Error: old_string must not be empty. Use the write tool to create or overwrite a file."
        if old_string == new_string:
            return "Error: old_string and new_string are identical - nothing to change."

        raw = filepath.read_bytes().decode("utf-8", errors="replace")
        style = _detect_line_ending(raw)
        # Match on a normalized LF "model view" so a pure-CRLF file can be
        # edited with an LF old_string (the read tool already shows LF). The
        # search/replace strings are normalized the same way to stay
        # consistent with that view.
        content = _to_model_view(raw, style)
        old_view = _to_model_view(old_string, style)
        new_view = _to_model_view(new_string, style)

        count = content.count(old_view)
        if count == 0:
            return (
                f"Error: old_string not found in {filename}. The file contents "
                "may be out of date - re-read the file and copy old_string from "
                "the read output."
            )
        if count > 1 and not replace_all:
            return (
                f"Error: old_string found {count} times in {filename} - it must "
                "be unique. Add more surrounding context to old_string, or set "
                "replace_all=true to replace every occurrence."
            )

        if replace_all:
            new_content = content.replace(old_view, new_view)
        else:
            new_content = content.replace(old_view, new_view, 1)

        # Write bytes to preserve the original line endings verbatim
        # (read_text/write_text default to universal-newline translation).
        filepath.write_bytes(_materialize(new_content, style).encode("utf-8"))

        diff_prev, diff_current = _build_context_diff(content, new_content, old_view)
        return {
            "result": f"File updated: {filename}",
            "diff_prev": diff_prev,
            "diff_current": diff_current,
        }


def _detect_line_ending(text: str) -> LineEndingStyle:
    """Classify line endings: pure CRLF, pure LF, or mixed (lone CR / both)."""
    has_crlf = False
    has_lf = False
    has_lone_cr = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                has_crlf = True
                i += 2
                continue
            has_lone_cr = True
        elif c == "\n":
            has_lf = True
        i += 1
    if has_lone_cr or (has_crlf and has_lf):
        return "mixed"
    if has_crlf:
        return "crlf"
    return "lf"


def _to_model_view(text: str, style: LineEndingStyle) -> str:
    """Normalize a pure-CRLF text to LF for matching; leave others as-is."""
    if style == "crlf":
        return text.replace("\r\n", "\n")
    return text


def _materialize(text: str, style: LineEndingStyle) -> str:
    """Restore the original line-ending style after editing the LF view."""
    if style == "crlf":
        return text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text


def _build_context_diff(old_content: str, new_content: str, old_str: str) -> tuple[str, str]:
    """Extract the changed region with surrounding context lines.

    Since new_content == old_content.replace(old_str, new_str, ...), the prefix
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

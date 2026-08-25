import difflib
from pathlib import Path
from typing import Any

from src.trilobite.file_access import detect_line_ending, materialize, resolve_file_path, to_model_view
from src.trilobite.tools.tool import Tool

_CONTEXT_LINES = 6


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
        session_tmp: Path | None = None,
        filename: str = "",
        old_string: str = "",
        new_string: str = "",
        replace_all: bool = False,
        **kwargs: Any,
    ) -> str | dict[str, Any]:
        filepath, error, perm_path = resolve_file_path(
            filename, working_dir, additional_dirs, session_tmp
        )
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
        style = detect_line_ending(raw)
        # Match on a normalized LF "model view" so a pure-CRLF file can be
        # edited with an LF old_string (the read tool already shows LF). The
        # search/replace strings are normalized the same way to stay
        # consistent with that view.
        content = to_model_view(raw, style)
        old_view = to_model_view(old_string, style)
        new_view = to_model_view(new_string, style)

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
        filepath.write_bytes(materialize(new_content, style).encode("utf-8"))

        diff_rows = _build_diff_rows(content, new_content, old_view)
        return {
            "result": f"File updated: {filename}",
            "diff": diff_rows,
        }


def _build_diff_rows(old_content: str, new_content: str, old_str: str) -> list[dict[str, Any]]:
    """Build a line-level unified diff of the changed region with real line numbers.

    A context window of ``_CONTEXT_LINES`` complete lines is taken around the
    replacement. Because ``new_content`` is ``old_content`` with the change
    applied in place, the text before the change is identical, so the window
    starts at the same absolute line number in both files. ``difflib`` then
    classifies each window line as equal/added/removed and we stamp it with its
    real (1-based) file line number -- ``old`` for lines in the original file,
    ``new`` for lines in the result.
    """
    old_len = len(old_content)
    pos = old_content.index(old_str)
    old_end = pos + len(old_str)
    size_delta = len(new_content) - old_len

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

    # Absolute (1-based) line number of the window's first line. The lines
    # before the change are identical in old and new, so this offsets both.
    line_start = old_content.count("\n", 0, ctx_start) + 1

    old_lines = old_content[ctx_start:ctx_end].splitlines()
    # The new window shares ctx_start; its end shifts by the size delta.
    new_ctx_end = min(ctx_end + size_delta, len(new_content))
    new_lines = new_content[ctx_start:new_ctx_end].splitlines()

    rows: list[dict[str, Any]] = []
    old_no = line_start
    new_no = line_start
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                rows.append({"type": "equal", "old": old_no, "new": new_no, "text": old_lines[k]})
                old_no += 1
                new_no += 1
        elif tag == "replace":
            for k in range(i1, i2):
                rows.append({"type": "removed", "old": old_no, "new": None, "text": old_lines[k]})
                old_no += 1
            for k in range(j1, j2):
                rows.append({"type": "added", "old": None, "new": new_no, "text": new_lines[k]})
                new_no += 1
        elif tag == "delete":
            for k in range(i1, i2):
                rows.append({"type": "removed", "old": old_no, "new": None, "text": old_lines[k]})
                old_no += 1
        elif tag == "insert":
            for k in range(j1, j2):
                rows.append({"type": "added", "old": None, "new": new_no, "text": new_lines[k]})
                new_no += 1
    return rows

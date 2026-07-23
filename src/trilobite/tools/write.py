from pathlib import Path
from typing import Any, Literal

from src.trilobite.file_access import resolve_file_path
from src.trilobite.tools.tool import Tool

WriteMode = Literal["overwrite", "append"]


class WriteTool(Tool):
    name = "write"
    description = (
        "Create, append to, or entirely replace a file. Missing parent "
        "directories are created automatically.\n"
        "Rules:\n"
        "- NOT allowed for incremental changes to an existing file, including "
        "trivial or one-line edits - use edit instead. Use write only when the "
        "file does not exist, you intend a complete replacement, or the new "
        "contents have little continuity with the old.\n"
        "- Read the file before overwriting an existing one.\n"
        "- mode overwrite (default) replaces the whole file; append adds "
        "content at EOF without adding a newline.\n"
        "- content is written literally, including the line endings you supply; "
        "do not include any line-number prefixes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Path to the file relative to working directory.",
            },
            "content": {
                "type": "string",
                "description": "The full content to write to the file.",
            },
            "mode": {
                "type": "string",
                "enum": ["overwrite", "append"],
                "default": "overwrite",
                "description": "overwrite replaces the whole file (default); append adds content at the end without adding a newline.",
            },
        },
        "required": ["filename", "content"],
    }

    def execute(
        self,
        working_dir: Path,
        session_dir: Path,
        additional_dirs: list[Path] | None = None,
        filename: str = "",
        content: str = "",
        mode: str = "overwrite",
        **kwargs: Any,
    ) -> str | dict[str, Any]:
        filepath, error, perm_path = resolve_file_path(filename, working_dir, additional_dirs)
        if perm_path:
            return {"result": error, "permission": perm_path}
        if error:
            return error

        if filepath.exists() and filepath.is_dir():
            return f"Error: {filename} is a directory"
        if mode not in ("overwrite", "append"):
            return f"Error: invalid mode '{mode}' - use 'overwrite' or 'append'."

        filepath.parent.mkdir(parents=True, exist_ok=True)

        if mode == "append":
            # Write bytes so supplied line endings are preserved verbatim
            # (write_text would translate newlines).
            with filepath.open("ab") as f:
                f.write(content.encode("utf-8"))
            return f"Appended to: {filename}"

        existed = filepath.exists()
        filepath.write_bytes(content.encode("utf-8"))
        action = "Created" if not existed else "Written"
        return f"{action}: {filename}"

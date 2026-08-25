import mimetypes
from pathlib import Path
from typing import Any

from src.trilobite.file_access import resolve_file_path
from src.trilobite.image_storage import format_mtime, save_image
from src.trilobite.messages import Image
from src.trilobite.tools.tool import Tool


class ReadTool(Tool):
    name = "read"
    description = (
        "Read content from a file in the working directory. Only the first "
        "50 lines / 10000 chars are returned by default; if the file is "
        "longer a trailing marker says so and how to continue -- increase "
        "limit_lines or limit_chars, or pass start_line to page forward."
    )
    image_description = (
        "Read content from a file in the working directory. Only the first "
        "50 lines / 10000 chars are returned by default; if the file is "
        "longer a trailing marker says so and how to continue -- increase "
        "limit_lines or limit_chars, or pass start_line to page forward. "
        "If the file is a supported image (PNG, JPEG, GIF, WebP), the tool "
        "stores the image in the session and returns a self-closing "
        "`<image .../>` marker. The actual image is then sent as a follow-up "
        "user message so the model can see it."
    )

    def to_openai_tool(self, enable_vl: bool = False) -> dict:
        d = super().to_openai_tool(enable_vl)
        if enable_vl:
            d["function"]["description"] = self.image_description
        return d

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
                "description": (
                    "Max lines to return (default 50). Increase to read more "
                    "of a long file."
                ),
            },
            "start_line": {
                "type": "integer",
                "default": 0,
                "description": (
                    "Line number to start from, 0-indexed (default 0). Use to "
                    "page through a file longer than limit_lines."
                ),
            },
            "limit_chars": {
                "type": "integer",
                "default": 10000,
                "description": (
                    "Max characters to return (default 10000). Increase if "
                    "content is cut off."
                ),
            },
        },
        "required": ["filename"],
    }

    def execute(
        self,
        working_dir: Path,
        session_dir: Path,
        additional_dirs: list[Path] | None = None,
        session_tmp: Path | None = None,
        filename: str = "",
        limit_lines: int = 50,
        start_line: int = 0,
        limit_chars: int = 10000,
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
            return f"Error: File not found: {filename}"
        if filepath.is_dir():
            return f"Error: {filename} is a directory"

        mime, _ = mimetypes.guess_type(str(filepath))
        if mime and mime.startswith("image/"):
            try:
                data = filepath.read_bytes()
            except Exception as e:
                return f"Error reading image: {e}"
            date = format_mtime(filepath.stat().st_mtime)
            image: Image = save_image(
                session_dir,
                data,
                mime,
                original_name=filepath.name,
                date=date,
            )
            marker = (
                f'<image filename="{image.filename}" '
                f'original_name="{image.original_name}" '
                f'mime="{image.mime_type}" '
                f'modified="{image.date}" />'
            )
            return {"result": marker, "image": image}

        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading file: {e}"

        lines = content.splitlines()
        sliced = lines[start_line : start_line + limit_lines]
        text = "\n".join(sliced)
        if len(text) > limit_chars:
            text = text[:limit_chars] + (
                f"\n... [truncated at {limit_chars} characters; increase "
                f"limit_chars to see more]"
            )
        end_line = start_line + len(sliced)
        if end_line < len(lines):
            text += (
                f"\n... [showing lines {start_line}-{end_line - 1} of "
                f"{len(lines)} (0-indexed); call read again with "
                f"start_line={end_line} or a larger limit_lines to continue]"
            )
        return text

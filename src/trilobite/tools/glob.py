from pathlib import Path
from typing import Any

from src.trilobite.file_access import resolve_file_path
from src.trilobite.file_discovery import discover_files, relpath
from src.trilobite.tools.tool import Tool


class GlobTool(Tool):
    name = "glob"
    description = (
        "Find files by name pattern under a directory. Returns matching paths "
        "(relative to the working directory) sorted by modification time, "
        "newest first. Respects .gitignore in git repos. Prefer this over "
        "bash `find` when looking for files by name."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Glob pattern, e.g. '**/*.py', 'src/**/*.ts', '*.md'. "
                    "Supports ** for recursive matching."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Directory to search in, relative to the working "
                    "directory. Defaults to the working directory root."
                ),
            },
            "limit": {
                "type": "integer",
                "default": 100,
                "description": "Max number of matches to return.",
            },
        },
        "required": ["pattern"],
    }

    def execute(
        self,
        working_dir: Path,
        session_dir: Path,
        additional_dirs: list[Path] | None = None,
        session_tmp: Path | None = None,
        pattern: str = "",
        path: str = "",
        limit: int = 100,
        **kwargs: Any,
    ) -> str:
        root, error, perm_path = resolve_file_path(
            path or ".", working_dir, additional_dirs, session_tmp
        )
        if perm_path:
            return f"Error: {error}"
        if error:
            return error
        if not root.exists():
            return f"Error: Directory not found: {path}"
        if not root.is_dir():
            return f"Error: {path} is not a directory"

        try:
            candidates = sorted(
                Path(root).glob(pattern),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception as e:
            return f"Error: Invalid pattern: {e}"

        # ``Path.glob`` walks the whole tree, including gitignored files; keep
        # only those ``discover_files`` (which honours .gitignore) also sees.
        visible = {p.resolve() for p in discover_files(root)}
        matches: list[str] = []
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if candidate.resolve() not in visible:
                continue
            matches.append(relpath(candidate, working_dir))
            if len(matches) >= limit:
                break

        if not matches:
            return "No files found."
        return "\n".join(matches)

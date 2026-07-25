import fnmatch
import re
from pathlib import Path
from typing import Any

from src.trilobite.file_access import resolve_file_path
from src.trilobite.file_discovery import discover_files, relpath
from src.trilobite.tools.tool import Tool


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents with a regular expression. Returns matching "
        "lines as 'path:line: content'. Respects .gitignore in git repos. "
        "Prefer this over bash grep for searching the codebase."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression (Python re syntax) to search for.",
            },
            "path": {
                "type": "string",
                "description": (
                    "File or directory to search in, relative to the working "
                    "directory. Defaults to the working directory root."
                ),
            },
            "glob": {
                "type": "string",
                "description": (
                    "File-name glob filter, e.g. '*.py'. Only files whose name "
                    "matches are searched."
                ),
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "default": "content",
                "description": (
                    "content = matching lines (default); files_with_matches = "
                    "only file paths with a match; count = match count per file."
                ),
            },
            "case_insensitive": {
                "type": "boolean",
                "default": False,
                "description": "Case-insensitive match.",
            },
            "context": {
                "type": "integer",
                "default": 0,
                "description": "Lines of context to show before and after each match (like grep -C).",
            },
            "max_results": {
                "type": "integer",
                "default": 100,
                "description": "Max number of result lines to return.",
            },
        },
        "required": ["pattern"],
    }

    def execute(
        self,
        working_dir: Path,
        session_dir: Path,
        additional_dirs: list[Path] | None = None,
        pattern: str = "",
        path: str = "",
        glob: str = "",
        output_mode: str = "content",
        case_insensitive: bool = False,
        context: int = 0,
        max_results: int = 100,
        **kwargs: Any,
    ) -> str:
        root, error, perm_path = resolve_file_path(
            path or ".", working_dir, additional_dirs
        )
        if perm_path:
            return f"Error: {error}"
        if error:
            return error
        if not root.exists():
            return f"Error: Path not found: {path}"

        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return f"Error: Invalid regex: {e}"

        if root.is_file():
            files = [root]
        else:
            files = [f for f in discover_files(root) if f.is_file()]
            if glob:
                files = [f for f in files if fnmatch.fnmatch(f.name, glob)]

        results: list[str] = []
        total_matches = 0
        for filepath in files:
            try:
                raw = filepath.read_bytes()
            except OSError:
                continue
            # Skip likely-binary files: a NUL byte in the first chunk means
            # this is not text worth grepping.
            if b"\x00" in raw[:8192]:
                continue
            text = raw.decode("utf-8", errors="replace")
            lines = text.splitlines()
            matched_lines = {
                i for i, line in enumerate(lines) if regex.search(line)
            }
            if not matched_lines:
                continue

            rel = relpath(filepath, working_dir)
            total_matches += len(matched_lines)

            if output_mode == "files_with_matches":
                results.append(rel)
            elif output_mode == "count":
                results.append(f"{rel}:{len(matched_lines)}")
            else:
                if context > 0:
                    want: set[int] = set()
                    for i in matched_lines:
                        want.update(
                            range(
                                max(0, i - context),
                                min(len(lines), i + context + 1),
                            )
                        )
                    for j in sorted(want):
                        marker = ":" if j in matched_lines else "-"
                        results.append(f"{rel}:{j + 1}{marker} {lines[j]}")
                else:
                    for i in sorted(matched_lines):
                        results.append(f"{rel}:{i + 1}: {lines[i]}")

            if len(results) >= max_results:
                break

        if not results:
            return "No matches found."
        output = "\n".join(results[:max_results])
        if total_matches > max_results and output_mode == "content":
            output += (
                f"\n... [{total_matches} matches total, "
                f"showing first {max_results} result lines]"
            )
        return output

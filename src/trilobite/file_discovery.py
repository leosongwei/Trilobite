"""File discovery for the search tools (``glob`` / ``grep``).

Both tools need to enumerate files under a directory while honouring
.gitignore. Doing this in pure Python (parsing nested .gitignore files) is
fiddly and slow, so in a git worktree we delegate to ``git ls-files`` which
already knows the ignore rules. Outside git we fall back to a plain
:func:`os.walk` that prunes the common noise/build directories.

This module is intentionally small and dependency-free so the search tools
stay fast to import.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Directories that are never useful to search and are slow to walk. Only used
#: by the non-git fallback -- inside a git worktree .gitignore is the source of
#: truth instead.
_NOISE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".eggs",
        ".next",
        ".nuxt",
        ".cache",
        ".sass-cache",
        "target",
        ".gradle",
    }
)


def discover_files(root: Path) -> list[Path]:
    """List regular files under ``root`` visible to the agent.

    In a git worktree this delegates to ``git ls-files`` so .gitignore rules
    are honoured (tracked files plus untracked-but-not-ignored files). Outside
    git it walks the tree with :func:`os.walk`, pruning the directories in
    :data:`_NOISE_DIRS`.

    Paths are returned in filesystem traversal order; callers sort as needed.
    """
    files = _git_visible_files(root)
    if files is not None:
        return files
    return _walk_files(root)


def _git_visible_files(root: Path) -> list[Path] | None:
    """Return tracked + untracked-non-ignored files via ``git ls-files``.

    Returns ``None`` when git is missing or ``root`` is not inside a worktree,
    so the caller can fall back to a plain walk. ``git -C root ls-files``
    outputs paths relative to ``root``, which we rejoin to absolute paths.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    files: list[Path] = []
    for name in result.stdout.split("\0"):
        if not name:
            continue
        path = root / name
        if path.is_file():
            files.append(path)
    return files


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS]
        for filename in filenames:
            files.append(Path(dirpath) / filename)
    return files


def relpath(path: Path, working_dir: Path) -> str:
    """Render ``path`` relative to ``working_dir`` when possible.

    Matches outside the working directory (e.g. under an additional authorized
    directory) fall back to the absolute path so the model can still tell them
    apart.
    """
    try:
        return str(path.relative_to(working_dir))
    except ValueError:
        return str(path)

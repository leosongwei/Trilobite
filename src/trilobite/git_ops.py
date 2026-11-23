"""Git helpers for the file manager UI.

Lightweight subprocess wrappers around the git CLI: per-directory file
listings with change status, branch lists and base-branch file contents.
Kept intentionally small and dependency-free (same style as
``file_discovery.py``); the heavy lifting (ignore rules, status) is delegated
to git itself so behaviour always matches the command line.
"""

from __future__ import annotations

import difflib
import os
import subprocess
from pathlib import Path
from typing import Any

from src.trilobite.file_discovery import _NOISE_DIRS

#: Max entries returned per directory listing; beyond this the listing is
#: truncated (guard against pathological directories such as an unignored
#: node_modules).
MAX_LIST_ENTRIES = 5000

#: Max diff rows produced for a single file (a 512 KB file can still exceed
#: this; beyond it the diff is refused to keep the frontend responsive).
MAX_DIFF_ROWS = 20000

#: Conventional default-branch names, used when the repo has no remote HEAD to
#: ask. Order matters: the first one that exists wins.
DEFAULT_BRANCH_NAMES = ("main", "master", "trunk")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``root``; stdout/stderr captured as text."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )


def is_git_repo(root: Path) -> bool:
    return _git(root, "rev-parse", "--is-inside-work-tree").returncode == 0


def list_branches(root: Path) -> list[str]:
    proc = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return [b for b in proc.stdout.splitlines() if b]


def current_branch(root: Path) -> str:
    return _git(root, "symbolic-ref", "--short", "HEAD").stdout.strip()


def ref_exists(root: Path, ref: str) -> bool:
    """Whether ``ref`` resolves to a commit (branch, tag, ``origin/main``, ``HEAD``)."""
    if not ref:
        return False
    return _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def _remote_head_branch(root: Path) -> str:
    """The remote's default branch as a usable ref (``origin/main``), or ``""``.

    ``refs/remotes/origin/HEAD`` is a symbolic ref kept by ``git clone`` /
    ``git remote set-head``; it is the most direct answer to "what is this
    repository's mainline".
    """
    ref = _git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD").stdout.strip()
    return ref if ref_exists(root, ref) else ""


def default_diff_base(root: Path) -> str:
    """The ref a workspace's changes are compared against.

    git has no command for "the branch this work forked from", so this resolves
    in order:

    1. the current branch when its name is itself a conventional default
       (``main``/``master``/``trunk``) -- diffing against its own tip then
       shows exactly the uncommitted work;
    2. the remote's HEAD branch (the authoritative mainline of a clone; a
       same-named local branch is preferred over ``origin/<name>``);
    3. otherwise the first existing conventional default branch -- a feature
       branch in a repo without a remote is compared against the mainline it
       forked from;
    4. otherwise the current branch, otherwise ``HEAD`` (detached).

    An empty string means there is nothing to diff against yet (a repository
    without commits) or the directory is not a git worktree.
    """
    current = current_branch(root)
    if current in DEFAULT_BRANCH_NAMES and ref_exists(root, current):
        return current
    local_branches = list_branches(root)
    remote_head = _remote_head_branch(root)
    if remote_head:
        short = remote_head.split("/", 1)[-1]
        return short if short in local_branches else remote_head
    for name in DEFAULT_BRANCH_NAMES:
        if name in local_branches:
            return name
    if ref_exists(root, current):
        return current
    return "HEAD" if ref_exists(root, "HEAD") else ""


def _map_status(xy: str) -> str:
    """Map a two-letter porcelain status to the UI status vocabulary."""
    if xy == "??":
        return "untracked"
    if "D" in xy:
        return "deleted"
    if xy[0] == "A":
        return "added"
    return "modified"


def _diff_status(xy: str) -> str:
    """Map a single-letter diff --name-status code to the UI vocabulary."""
    return {"A": "added", "M": "modified", "D": "deleted"}.get(xy, "modified")


def list_dir(root: Path, relpath: str = "", base: str | None = None) -> dict[str, Any]:
    """List one directory's direct entries with change status.

    Returns ``{"entries", "is_git_repo", "current_branch", "branches",
    "truncated"}``. In a git worktree the listing is built from ``git
    ls-files`` (tracked + untracked non-ignored), so ignored directories
    (e.g. ``.venv``, ``node_modules``) never appear; change status comes from
    ``git status --porcelain``. When ``base`` is given, the status reflects
    the working tree vs that branch instead (``git diff --name-status``) --
    used by the file manager's diff mode; directories whose subtree contains
    any changed file are themselves marked. Outside git we fall back to
    ``os.listdir`` with noise-directory pruning and every file is marked
    ``untracked``.
    """
    if is_git_repo(root):
        return _list_dir_git(root, relpath, base)
    return _list_dir_plain(root, relpath)


def _list_dir_git(root: Path, relpath: str, base: str | None) -> dict[str, Any]:
    dirpath = relpath if relpath else "."
    prefix = f"{relpath}/" if relpath else ""

    # Tracked + untracked non-ignored files under the directory (recursive).
    # ``-z`` is essential: without it git C-style quotes non-ASCII paths
    # (``core.quotePath``), so Chinese filenames would come back escaped and
    # never match the raw paths from ``status --porcelain -z`` below.
    files: set[str] = set()
    untracked: set[str] = set()
    for args, is_untracked in (
        (("-z", "--cached"), False),
        (("-z", "-o", "--exclude-standard"), True),
    ):
        proc = _git(root, "ls-files", *args, "--", dirpath)
        lines = [s for s in proc.stdout.split("\0") if s]
        files.update(lines)
        if is_untracked:
            untracked.update(lines)

    entries: dict[str, dict[str, Any]] = {}
    for f in files:
        if not f.startswith(prefix):
            continue
        parts = f[len(prefix):].split("/", 1)
        name = parts[0]
        entries.setdefault(name, {"name": name, "is_dir": len(parts) > 1})

    # Change status per direct child (also surfaces deleted files, which are
    # absent from ls-files when the deletion is staged).
    status_map: dict[str, str] = {}
    proc = _git(root, "status", "--porcelain", "-z", "--no-renames", "--", dirpath)
    recs = proc.stdout.split("\0")
    for rec in recs:
        if len(rec) < 4:
            continue
        path = rec[3:]
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if "/" in rest:
            entries.setdefault(rest.split("/", 1)[0], {"name": rest.split("/", 1)[0], "is_dir": True})
        elif rest:
            entries.setdefault(rest, {"name": rest, "is_dir": False})
            status_map[rest] = _map_status(rec[:2])

    # Diff mode: status vs a base branch instead of vs HEAD. Changed files are
    # marked by `git diff --name-status`; directories whose subtree contains a
    # change are marked too (so unexpanded folders still show up). Untracked
    # files have no baseline in the branch: treat them as added.
    if base:
        base_status: dict[str, str] = {}
        changed_dirs: set[str] = set()
        proc = _git(root, "diff", "--name-status", "-z", "--no-renames", base, "--", dirpath)
        if proc.returncode == 0:
            recs = proc.stdout.split("\0")
            i = 0
            while i + 1 < len(recs):
                xy = recs[i]
                path = recs[i + 1]
                i += 2
                if not xy or not path.startswith(prefix):
                    continue
                rest = path[len(prefix):]
                if "/" in rest:
                    changed_dirs.add(rest.split("/", 1)[0])
                elif rest:
                    entries.setdefault(rest, {"name": rest, "is_dir": False})
                    base_status[rest] = _diff_status(xy)
            for f in untracked:
                if f.startswith(prefix) and "/" not in f[len(prefix):]:
                    base_status[f[len(prefix):]] = "added"
        for name, entry in entries.items():
            if entry["is_dir"]:
                entry["status"] = "modified" if name in changed_dirs else "clean"
                continue
            p = root / relpath / name
            try:
                st = p.stat()
                entry["size"] = st.st_size
                entry["mtime"] = int(st.st_mtime)
            except OSError:
                pass  # deleted file: no stat available
            entry["status"] = base_status.get(name, "clean")
        return _finalize_listing(root, entries)

    for name, entry in entries.items():
        if entry["is_dir"]:
            continue
        p = root / relpath / name
        try:
            st = p.stat()
            entry["size"] = st.st_size
            entry["mtime"] = int(st.st_mtime)
        except OSError:
            pass  # deleted file: no stat available
        entry["status"] = status_map.get(name, "clean")

    return _finalize_listing(root, entries)


def _list_dir_plain(root: Path, relpath: str) -> dict[str, Any]:
    entries: dict[str, dict[str, Any]] = {}
    try:
        names = sorted(os.listdir(root / relpath), key=str.lower)
    except OSError:
        names = []
    for name in names:
        p = root / relpath / name
        if p.is_dir():
            if name in _NOISE_DIRS:
                continue
            entries[name] = {"name": name, "is_dir": True}
        elif p.is_file():
            try:
                st = p.stat()
            except OSError:
                continue
            entries[name] = {
                "name": name,
                "is_dir": False,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "status": "untracked",
            }
    return _finalize_listing(root, entries, is_git=False)


def _finalize_listing(
    root: Path, entries: dict[str, dict[str, Any]], is_git: bool = True
) -> dict[str, Any]:
    names = list(entries)
    truncated = len(names) > MAX_LIST_ENTRIES
    if truncated:
        names = names[:MAX_LIST_ENTRIES]
    ordered = sorted(names, key=lambda n: (not entries[n]["is_dir"], n.lower()))
    return {
        "entries": [entries[n] for n in ordered],
        "is_git_repo": is_git,
        "current_branch": current_branch(root) if is_git else "",
        "branches": list_branches(root) if is_git else [],
        "truncated": truncated,
    }


def show_base_content(root: Path, base: str, relpath: str) -> tuple[str | None, str | None]:
    """Return the file's content at ``base``, or ``(None, error)``.

    ``base`` may be any ref (branch, remote branch, tag, ``HEAD``).
    ``(None, None)`` means the ref exists but the file is not in it
    (untracked/added file) -- the caller treats the baseline as empty.
    """
    if not ref_exists(root, base):
        return None, f"branch not found: {base}"
    proc = _git(root, "show", f"{base}:{relpath}")
    if proc.returncode != 0:
        return None, None
    return proc.stdout, None


def build_diff_rows(base_content: str | None, current_content: str) -> list[dict[str, Any]]:
    """Line-level diff of the whole file, in the ``DiffRow`` format consumed
    by the frontend ``DiffView`` (``{type: equal|added|removed, old, new,
    text}`` with 1-based line numbers).
    """
    base_lines = (base_content or "").splitlines()
    cur_lines = current_content.splitlines()
    matcher = difflib.SequenceMatcher(a=base_lines, b=cur_lines, autojunk=False)
    rows: list[dict[str, Any]] = []
    old_no = new_no = 1
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for line in base_lines[i1:i2]:
                rows.append({"type": "equal", "old": old_no, "new": new_no, "text": line})
                old_no += 1
                new_no += 1
        elif tag == "delete":
            for line in base_lines[i1:i2]:
                rows.append({"type": "removed", "old": old_no, "new": None, "text": line})
                old_no += 1
        elif tag == "insert":
            for line in cur_lines[j1:j2]:
                rows.append({"type": "added", "old": None, "new": new_no, "text": line})
                new_no += 1
        else:  # replace
            for line in base_lines[i1:i2]:
                rows.append({"type": "removed", "old": old_no, "new": None, "text": line})
                old_no += 1
            for line in cur_lines[j1:j2]:
                rows.append({"type": "added", "old": None, "new": new_no, "text": line})
                new_no += 1
    return rows

from __future__ import annotations

from pathlib import Path
from typing import Literal

_SENSITIVE_BASES = ("id_rsa", "id_ed25519", "id_ecdsa", "credentials")
_SENSITIVE_SUFFIXES = (
    ".bak", ".backup", ".copy", ".disabled", ".key",
    ".old", ".orig", ".pem", ".save", ".tmp",
)
_SENSITIVE_SEPARATORS = ("", "-", "_", ".")
_EXEMPT_NAMES = {".env.example", ".env.sample", ".env.template"}
_SENSITIVE_PATH_SUFFIXES = (".aws/credentials", ".gcp/credentials")

#: Line-ending style of a text file, used when editing/saving content.
LineEndingStyle = Literal["lf", "crlf", "mixed"]


def detect_line_ending(text: str) -> LineEndingStyle:
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


def to_model_view(text: str, style: LineEndingStyle) -> str:
    """Normalize a pure-CRLF text to LF for matching; leave others as-is."""
    if style == "crlf":
        return text.replace("\r\n", "\n")
    return text


def materialize(text: str, style: LineEndingStyle) -> str:
    """Restore the original line-ending style after editing the LF view."""
    if style == "crlf":
        return text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text


def is_sensitive_file(path: Path) -> bool:
    name = path.name.lower()
    path_str = str(path).lower()

    if name in _EXEMPT_NAMES:
        return False

    if name.endswith(".pub"):
        base = name[:-4]
        if base in _SENSITIVE_BASES:
            return False

    if name == ".env" or name.startswith(".env."):
        return True

    for suffix in _SENSITIVE_PATH_SUFFIXES:
        if path_str.endswith(suffix):
            return True

    for base in _SENSITIVE_BASES:
        if name == base:
            return True
        for sep in _SENSITIVE_SEPARATORS:
            for suffix in _SENSITIVE_SUFFIXES:
                if name == f"{base}{sep}{suffix.lstrip('.')}":
                    return True

    return False


def normalize_dir(path: str, base: Path | None = None) -> Path:
    """Canonicalize an allowed-directory grant to its absolute form.

    ``~`` is expanded and symlinks resolved; relative paths resolve against
    *base* (the session's working dir) instead of the server's CWD. Grants
    are persisted and compared in this canonical form so that ``/foo/``,
    ``/foo`` and ``~/foo`` can never coexist as separate entries.
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (base or Path.cwd()) / p
    return p.resolve()


def normalize_dirs(dirs: list[str], base: Path | None = None) -> list[Path]:
    """Normalize a list of directory grants, deduped in first-seen order."""
    out: list[Path] = []
    for d in dirs or []:
        resolved = normalize_dir(d, base)
        if resolved not in out:
            out.append(resolved)
    return out


#: The model's ``/tmp`` is the session's scratch directory (bound onto /tmp
#: inside the bash sandbox), not the host's shared /tmp. File tools remap
#: absolute paths under this host path into the session scratch so bash and
#: the file tools share one namespace.
_HOST_TMP = Path("/tmp")


def resolve_file_path(
    filename: str,
    working_dir: Path,
    additional_dirs: list[Path] | None = None,
    session_tmp: Path | None = None,
) -> tuple[Path | None, str | None, str | None]:
    """Resolve and validate a file path.

    *session_tmp* is the session's scratch directory (``session_dir/tmp``).
    When provided, absolute paths under the host's ``/tmp`` are remapped into
    it -- the bash sandbox binds that directory onto ``/tmp``, so the file
    tools see the same ``/tmp`` the sandboxed bash does. Without it (bash
    unsandboxed, host /tmp) the paths keep their host meaning. Either way the
    ``/tmp`` area is implicitly allowed: no permission request, never
    persisted, never shown in the UI.

    Returns:
        (resolved_path, None, None) on success.
        (None, error_message, None) on hard error.
        (None, error_message, permission_path) when access can be granted by user.
    """
    if additional_dirs is None:
        additional_dirs = []

    is_absolute = filename.startswith("~") or Path(filename).is_absolute()

    if is_absolute:
        filepath = Path(filename).expanduser().resolve()
    else:
        filepath = (working_dir / filename).resolve()

    if is_sensitive_file(filepath):
        return None, f"Error: Access to sensitive file denied: {filename}", None

    if filepath.is_relative_to(_HOST_TMP):
        if session_tmp is not None:
            filepath = session_tmp / filepath.relative_to(_HOST_TMP)
        return filepath, None, None

    all_dirs = [working_dir] + additional_dirs
    in_workspace = any(filepath.is_relative_to(d) for d in all_dirs)

    if not in_workspace:
        parent = str(filepath.parent)
        msg = f"Access needed outside workspace: {filename}"
        return None, msg, parent

    return filepath, None, None

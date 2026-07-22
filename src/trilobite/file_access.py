from __future__ import annotations

from pathlib import Path

_SENSITIVE_BASES = ("id_rsa", "id_ed25519", "id_ecdsa", "credentials")
_SENSITIVE_SUFFIXES = (
    ".bak", ".backup", ".copy", ".disabled", ".key",
    ".old", ".orig", ".pem", ".save", ".tmp",
)
_SENSITIVE_SEPARATORS = ("", "-", "_", ".")
_EXEMPT_NAMES = {".env.example", ".env.sample", ".env.template"}
_SENSITIVE_PATH_SUFFIXES = (".aws/credentials", ".gcp/credentials")


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


def resolve_file_path(
    filename: str,
    working_dir: Path,
    additional_dirs: list[Path] | None = None,
) -> tuple[Path | None, str | None]:
    """Resolve and validate a file path.

    Returns (resolved_path, None) on success, (None, error_message) on failure.
    """
    if additional_dirs is None:
        additional_dirs = []

    is_absolute = filename.startswith("~") or Path(filename).is_absolute()

    if is_absolute:
        filepath = Path(filename).expanduser().resolve()
    else:
        filepath = (working_dir / filename).resolve()

    if is_sensitive_file(filepath):
        return None, f"Error: Access to sensitive file denied: {filename}"

    all_dirs = [working_dir] + additional_dirs
    in_workspace = any(filepath.is_relative_to(d) for d in all_dirs)

    if not in_workspace and not is_absolute:
        return None, (
            f"Error: Path escapes working directory: {filename}. "
            "Use an absolute path to access files outside the workspace."
        )

    return filepath, None

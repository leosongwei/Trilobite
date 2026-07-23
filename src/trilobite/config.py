import os
import shutil
from pathlib import Path

import yaml

DEFAULT_MAX_CONTEXT_TOKENS = 1_048_576
DEFAULT_MAX_TOKENS = 65_536

DEFAULT_CONFIG = {
    "model": "deepseek-chat",
    "api_key": "",
    "api_url": "https://api.deepseek.com/v1",
    "reasoning_effort": "max",
    "max_context_tokens": str(DEFAULT_MAX_CONTEXT_TOKENS),
    "max_tokens": str(DEFAULT_MAX_TOKENS),
    "log_level": "WARNING",
    "compaction_trigger_ratio": "0.7",
}


def get_project_root() -> Path:
    return Path(__file__).parent.parent.parent


def get_config_dir() -> Path:
    """User config directory: ${XDG_CONFIG_HOME:-~/.config}/trilobite."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "trilobite"


def init_config() -> dict:
    """Ensure the user config dir exists, then load config.

    On first run the config dir is seeded from the bundled ``config_example/``
    (or migrated from a legacy project-local ``config/`` if present). The
    config lives at ``${XDG_CONFIG_HOME:-~/.config}/trilobite``.
    """
    config_dir = get_config_dir()
    if not config_dir.exists():
        # Seed from a legacy project-local ``config/`` if present (dev mode),
        # otherwise from the bundled ``config_example/`` shipped inside the
        # package. Using ``__file__`` (not the project root) keeps this working
        # after ``pip install``, where the project root no longer exists.
        root = get_project_root()
        legacy_dir = root / "config"
        bundled = Path(__file__).parent / "config_example"
        src = legacy_dir if legacy_dir.exists() else bundled
        shutil.copytree(src, config_dir)

    config_path = config_dir / "config.yaml"
    config = dict(DEFAULT_CONFIG)
    if config_path.exists():
        with open(config_path) as f:
            user_config = yaml.safe_load(f) or {}
            config.update(user_config)

    return config


def load_system_prompt() -> str:
    return _load_prompt("system_prompt.txt", "You are a helpful coding agent.")


def load_compaction_prompt() -> str:
    return _load_prompt(
        "compaction_prompt.txt",
        "Summarize the conversation history, preserving critical information. Output text only.",
    )


def load_subagent_role_prefix() -> str:
    """Common prefix prepended to every subagent's system prompt."""
    return _load_prompt(
        "subagent_role_prefix.txt",
        (
            "You are running as a subagent. All user messages come from the main "
            "agent or user steering. The main agent cannot see your context; it "
            "only sees your final message when you finish. Treat the main agent "
            "as your caller. Do not ask the end user questions directly; if "
            "something is unclear, say so in your summary. You are a bounded "
            "task: finish with a concise summary of what you found or did."
        ),
    )


def load_subagent_role_prompt(subagent_type: str) -> str:
    """Role-specific guidance appended after the common prefix."""
    return _load_prompt(
        f"subagent_{subagent_type}.txt",
        {
            "explore": (
                "You are a read-only code exploration subagent. You can read files "
                "and run read-only shell commands (grep, find, ls, git log, etc.). "
                "Do not modify anything. Map out the relevant code and report "
                "exact file paths, line numbers, and how things connect."
            ),
            "general": (
                "You are a general-purpose subagent. You can read, edit, and run "
                "shell commands to complete the assigned sub-task. Make minimal, "
                "scoped changes. When done, summarize what you changed and why."
            ),
        }.get(subagent_type, ""),
    )


def _load_prompt(filename: str, fallback: str) -> str:
    prompt_path = get_config_dir() / filename
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return fallback


def get_sessions_dir() -> Path:
    sessions_dir = get_config_dir() / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir
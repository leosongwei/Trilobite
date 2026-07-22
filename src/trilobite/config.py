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
    "compaction_trigger_ratio": "0.7",
}


def get_project_root() -> Path:
    return Path(__file__).parent.parent.parent


def init_config() -> dict:
    """Copy config_example to config if it doesn't exist, then load config."""
    root = get_project_root()
    config_dir = root / "config"
    example_dir = root / "config_example"

    if not config_dir.exists():
        shutil.copytree(example_dir, config_dir)

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


def _load_prompt(filename: str, fallback: str) -> str:
    root = get_project_root()
    config_dir = root / "config"
    prompt_path = config_dir / filename
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return fallback


def get_sessions_dir() -> Path:
    root = get_project_root()
    sessions_dir = root / "config" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir

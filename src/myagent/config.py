import os
import shutil
from pathlib import Path

import yaml


DEFAULT_CONFIG = {
    "model": "deepseek-chat",
    "api_key": "",
    "api_url": "https://api.deepseek.com/v1",
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
    root = get_project_root()
    config_dir = root / "config"
    prompt_path = config_dir / "system_prompt.txt"
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return "You are a helpful coding agent."


def get_sessions_dir() -> Path:
    root = get_project_root()
    sessions_dir = root / "config" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir

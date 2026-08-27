import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_MAX_CONTEXT_TOKENS = 1_048_576
DEFAULT_MAX_TOKENS = 65_536

# Per-model defaults (used when a ``models`` entry omits a field).
DEFAULT_MODEL_MAX_CONTEXT = 400_000
DEFAULT_MODEL_MAX_TOKENS = 65_536
DEFAULT_MODEL_COMPACTION_RATIO = 0.7

DEFAULT_CONFIG = {
    "model": "deepseek-chat",
    "api_key": "",
    "api_url": "https://api.deepseek.com/v1",
    "reasoning_effort": "max",
    "max_context_tokens": str(DEFAULT_MAX_CONTEXT_TOKENS),
    "max_tokens": str(DEFAULT_MAX_TOKENS),
    "host": "127.0.0.1",
    "port": "2345",
    "log_level": "WARNING",
    "compaction_trigger_ratio": "0.7",
    "enable_vl": False,
    "skill_dirs": [],
    "allowed_dirs": [],
    "bash_sandbox": "auto",
    "max_stream_retries": 3,
    "models": [],
    "default_model": "",
    "default_vl_model": "",
}


@dataclass
class Model:
    """A predefined model definition from the ``models`` config list."""

    name: str  # display name shown in the UI
    model: str  # model name sent to the API
    api_key: str
    api_url: str
    enable_vl: bool = False
    max_context: int = DEFAULT_MODEL_MAX_CONTEXT
    max_tokens: int = DEFAULT_MODEL_MAX_TOKENS
    compaction_trigger_ratio: float = DEFAULT_MODEL_COMPACTION_RATIO
    extra_body: dict | None = None  # extra fields merged into the request body
    pretend_to_be_opencode: bool = True  # send opencode-style headers (User-Agent, session ids)

    def to_frontend_dict(self) -> dict:
        """Frontend-facing shape (never includes the api_key or extra_body)."""
        return {
            "name": self.name,
            "model": self.model,
            "api_url": self.api_url,
            "enable_vl": self.enable_vl,
            "max_context": self.max_context,
            "max_tokens": self.max_tokens,
            "compaction_trigger_ratio": self.compaction_trigger_ratio,
        }


def load_models(config: dict) -> list[Model]:
    """Build the model list from config.

    Prefers the ``models`` list; falls back to the legacy top-level
    model/api_key/api_url/... fields as a single model so old configs keep
    working without a ``models`` section.
    """
    models: list[Model] = []
    for m in config.get("models", []) or []:
        models.append(Model(
            name=m.get("name", m.get("model", "default")),
            model=m.get("model", m.get("name", "default")),
            api_key=m.get("api_key", config.get("api_key", "")),
            api_url=m.get("api_url", config.get("api_url", "")),
            enable_vl=bool(m.get("enable_vl", False)),
            max_context=int(m.get("max_context", DEFAULT_MODEL_MAX_CONTEXT)),
            max_tokens=int(m.get("max_tokens", DEFAULT_MODEL_MAX_TOKENS)),
            compaction_trigger_ratio=float(m.get("compaction_trigger_ratio", DEFAULT_MODEL_COMPACTION_RATIO)),
            extra_body=m.get("extra_body") or None,
            pretend_to_be_opencode=bool(m.get("pretend_to_be_opencode", True)),
        ))
    if not models:
        # Legacy fallback: a single model synthesized from the top-level
        # fields. The old top-level ``reasoning_effort`` becomes the model's
        # extra_body so pre-``models`` configs keep their thinking behavior.
        legacy_extra = {}
        if config.get("reasoning_effort"):
            legacy_extra["reasoning_effort"] = config.get("reasoning_effort")
        models.append(Model(
            name=config.get("model", "default"),
            model=config.get("model", "default"),
            api_key=config.get("api_key", ""),
            api_url=config.get("api_url", ""),
            enable_vl=bool(config.get("enable_vl", False)),
            max_context=int(config.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)),
            max_tokens=int(config.get("max_tokens", DEFAULT_MAX_TOKENS)),
            compaction_trigger_ratio=float(config.get("compaction_trigger_ratio", DEFAULT_MODEL_COMPACTION_RATIO)),
            extra_body=legacy_extra or None,
            pretend_to_be_opencode=True,
        ))
    return models


def get_model(config: dict, name: str) -> Model:
    """Return the model definition named ``name`` (raises KeyError if unknown)."""
    for m in load_models(config):
        if m.name == name:
            return m
    raise KeyError(name)


def get_default_model_name(config: dict) -> str:
    """Name of the session's default model: the ``default_model`` config key
    when it names a known model, otherwise the first entry in ``models``."""
    models = load_models(config)
    default = config.get("default_model")
    if default and any(m.name == default for m in models):
        return default
    return models[0].name


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


def get_sessions_dir() -> Path:
    sessions_dir = get_config_dir() / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir
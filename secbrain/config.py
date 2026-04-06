"""~/.secbrain/config.json management."""

import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".secbrain"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load() -> dict | None:
    """Load config from ~/.secbrain/config.json. Returns None if not found."""
    if not CONFIG_FILE.exists():
        return None
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save(cfg: dict) -> dict:
    """Save config to ~/.secbrain/config.json. Creates directory if needed."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    return cfg


def current_project(cfg: dict) -> str | None:
    """Return the project name for the current working directory."""
    if not cfg:
        return None
    cwd = str(Path.cwd())
    return cfg.get("projects", {}).get(cwd)


def register_project(cfg: dict, project_name: str) -> dict:
    """Register current directory as project_name in config."""
    cfg.setdefault("projects", {})[str(Path.cwd())] = project_name
    return save(cfg)

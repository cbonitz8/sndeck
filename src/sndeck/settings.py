"""sndeck app settings: config file (~/.config/sndeck/config.toml) + precedence resolvers."""
from __future__ import annotations

import os
import tomllib

DEFAULT_CONFIG_PATH = "~/.config/sndeck/config.toml"


def load_sndeck_config(path: str | None = None) -> dict:
    """Load the sndeck TOML config; {} if absent. Env SNDECK_CONFIG overrides the path."""
    p = os.path.expanduser(path or os.environ.get("SNDECK_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        with open(p, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}


def resolve_scratch(argv: list[str], environ, cfg: dict, cwd: str) -> str:
    """Precedence: CLI positional arg > SNDECK_SCRATCH env > config scratch_dir > cwd."""
    if len(argv) > 1 and not argv[1].startswith("-"):
        val = argv[1]
    else:
        val = environ.get("SNDECK_SCRATCH") or cfg.get("scratch_dir") or cwd
    return os.path.expanduser(val)


def resolve_instance(environ, cfg: dict) -> str:
    """Precedence: SNDECK_INSTANCE env > config instance > 'dev'."""
    return environ.get("SNDECK_INSTANCE") or cfg.get("instance") or "dev"


def resolve_theme(environ, cfg: dict) -> str:
    """Precedence: SNDECK_THEME env > config theme > 'dark'.
    Accepts 'dark'/'light' aliases or a direct Textual theme name."""
    from .theme import MACCHIATO, LATTE
    val = environ.get("SNDECK_THEME") or cfg.get("theme") or "dark"
    return {"dark": MACCHIATO, "light": LATTE}.get(val, val)

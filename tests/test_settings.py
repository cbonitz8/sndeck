import os
from sndeck.settings import load_sndeck_config, resolve_scratch, resolve_instance


def test_load_config_missing_returns_empty(tmp_path):
    assert load_sndeck_config(str(tmp_path / "nope.toml")) == {}


def test_load_config_reads_toml(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('scratch_dir = "/x/y"\ninstance = "dev"\n')
    cfg = load_sndeck_config(str(p))
    assert cfg["scratch_dir"] == "/x/y" and cfg["instance"] == "dev"


def test_resolve_scratch_precedence():
    cfg = {"scratch_dir": "/from/config"}
    assert resolve_scratch(["sndeck", "/from/arg"], {}, cfg, "/cwd") == "/from/arg"
    assert resolve_scratch(["sndeck"], {"SNDECK_SCRATCH": "/from/env"}, cfg, "/cwd") == "/from/env"
    assert resolve_scratch(["sndeck"], {}, cfg, "/cwd") == "/from/config"
    assert resolve_scratch(["sndeck"], {}, {}, "/cwd") == "/cwd"
    assert resolve_scratch(["sndeck", "--help"], {}, {}, "/cwd") == "/cwd"  # flags not treated as scratch


def test_resolve_scratch_expanduser():
    assert resolve_scratch(["sndeck"], {}, {"scratch_dir": "~/x"}, "/cwd") == os.path.expanduser("~/x")


def test_resolve_instance_precedence():
    assert resolve_instance({"SNDECK_INSTANCE": "prod"}, {"instance": "dev"}) == "prod"
    assert resolve_instance({}, {"instance": "qa"}) == "qa"
    assert resolve_instance({}, {}) == "dev"


def test_resolve_theme_precedence():
    from sndeck.settings import resolve_theme
    from sndeck.theme import MACCHIATO, LATTE
    assert resolve_theme({"SNDECK_THEME": "light"}, {"theme": "dark"}) == LATTE
    assert resolve_theme({}, {"theme": "light"}) == LATTE
    assert resolve_theme({}, {"theme": "dark"}) == MACCHIATO
    assert resolve_theme({}, {}) == MACCHIATO
    assert resolve_theme({}, {"theme": "catppuccin-latte"}) == LATTE  # direct name passes through

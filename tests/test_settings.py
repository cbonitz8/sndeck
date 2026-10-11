import os
from sndeck.settings import load_sndeck_config, resolve_instance, resolve_store_root


def test_load_config_missing_returns_empty(tmp_path):
    assert load_sndeck_config(str(tmp_path / "nope.toml")) == {}


def test_load_config_reads_toml(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('scratch_dir = "/x/y"\ninstance = "dev"\n')
    cfg = load_sndeck_config(str(p))
    assert cfg["scratch_dir"] == "/x/y" and cfg["instance"] == "dev"


def test_resolve_store_root_sources(monkeypatch):
    from sndeck.settings import resolve_store_root
    assert resolve_store_root(["sndeck", "/x"], {}, {})[1] == "argument"
    assert resolve_store_root(["sndeck"], {"SNDECK_SCRATCH": "/e"}, {}) == ("/e", "SNDECK_SCRATCH")
    assert resolve_store_root(["sndeck"], {}, {"scratch_dir": "/c"}) == ("/c", "config scratch_dir")
    path, src = resolve_store_root(["sndeck"], {}, {})
    assert src == "default" and path.endswith("/.sn-scratch")


def test_resolve_store_root_precedence():
    cfg = {"scratch_dir": "/from/config"}
    env = {"SNDECK_SCRATCH": "/from/env"}
    assert resolve_store_root(["sndeck", "/from/arg"], env, cfg) == ("/from/arg", "argument")
    assert resolve_store_root(["sndeck"], env, cfg) == ("/from/env", "SNDECK_SCRATCH")
    assert resolve_store_root(["sndeck", "--help"], {}, {})[1] == "default"


def test_resolve_store_root_expanduser():
    assert resolve_store_root(["sndeck"], {}, {"scratch_dir": "~/x"})[0] == os.path.expanduser("~/x")
    assert resolve_store_root(["sndeck"], {}, {})[0] == os.path.expanduser("~/.sn-scratch")


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

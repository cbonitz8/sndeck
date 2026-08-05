import json
from sndeck.config import load_instance, _resolve_config_path

_CONFIG_ENVS = ("SNDECK_INSTANCES_CONFIG", "SNDECK_FORK_CONFIG", "HAPPY_CONFIG_PATH")


def _clear(monkeypatch):
    for e in _CONFIG_ENVS:
        monkeypatch.delenv(e, raising=False)


def test_load_instance_reads_fork_config(tmp_path):
    cfg = tmp_path / "servicenow-instances.json"
    cfg.write_text(json.dumps({"instances": [
        {"name": "dev", "url": "https://acmedev.service-now.com",
         "clientId": "abc123", "tokenUrl": "https://acmedev.service-now.com/oauth_token.do"},
    ]}))
    inst = load_instance("dev", config_path=str(cfg), account="user@acmedev")
    assert inst.name == "dev"
    assert inst.url == "https://acmedev.service-now.com"
    assert inst.client_id == "abc123"
    assert inst.token_url == "https://acmedev.service-now.com/oauth_token.do"
    assert inst.account == "user@acmedev"


def test_account_defaults_to_instance_name(tmp_path, monkeypatch):
    monkeypatch.delenv("SNDECK_ACCOUNT", raising=False)
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"instances": [
        {"name": "dev", "url": "https://x", "clientId": "k"}]}))
    inst = load_instance("dev", config_path=str(cfg))
    assert inst.account == "dev"


def test_token_url_defaults_to_url_slash_oauth_token(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"instances": [
        {"name": "dev", "url": "https://x.service-now.com", "clientId": "k"},
    ]}))
    inst = load_instance("dev", config_path=str(cfg), account="cbonitz@x")
    assert inst.token_url == "https://x.service-now.com/oauth_token.do"


def test_unknown_instance_raises(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"instances": [{"name": "dev", "url": "https://x", "clientId": "k"}]}))
    try:
        load_instance("prod", config_path=str(cfg), account="a@b")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_resolver_explicit_arg_wins_over_env(tmp_path, monkeypatch):
    _clear(monkeypatch)
    explicit = tmp_path / "explicit.json"; explicit.write_text("{}")
    env_file = tmp_path / "env.json"; env_file.write_text("{}")
    monkeypatch.setenv("SNDECK_INSTANCES_CONFIG", str(env_file))
    assert _resolve_config_path(str(explicit)) == str(explicit)


def test_resolver_prefers_instances_config_env(tmp_path, monkeypatch):
    _clear(monkeypatch)
    f = tmp_path / "i.json"; f.write_text("{}")
    monkeypatch.setenv("SNDECK_INSTANCES_CONFIG", str(f))
    assert _resolve_config_path() == str(f)


def test_resolver_instances_config_env_wins_over_fork_config_and_happy(tmp_path, monkeypatch):
    _clear(monkeypatch)
    i = tmp_path / "i.json"; i.write_text("{}")
    fork = tmp_path / "fork.json"; fork.write_text("{}")
    happy = tmp_path / "happy.json"; happy.write_text("{}")
    monkeypatch.setenv("SNDECK_INSTANCES_CONFIG", str(i))
    monkeypatch.setenv("SNDECK_FORK_CONFIG", str(fork))
    monkeypatch.setenv("HAPPY_CONFIG_PATH", str(happy))
    assert _resolve_config_path() == str(i)


def test_resolver_falls_back_to_legacy_fork_config_env(tmp_path, monkeypatch):
    _clear(monkeypatch)
    f = tmp_path / "fork.json"; f.write_text("{}")
    monkeypatch.setenv("SNDECK_FORK_CONFIG", str(f))
    assert _resolve_config_path() == str(f)


def test_resolver_honors_happy_config_path(tmp_path, monkeypatch):
    _clear(monkeypatch)
    f = tmp_path / "h.json"; f.write_text("{}")
    monkeypatch.setenv("HAPPY_CONFIG_PATH", str(f))
    assert _resolve_config_path() == str(f)


def test_resolver_falls_back_to_existing_candidate(tmp_path, monkeypatch):
    _clear(monkeypatch)
    # point both candidates into tmp; create only the sndeck-own one
    import sndeck.config as cfg
    own = tmp_path / "sndeck.json"; own.write_text("{}")
    monkeypatch.setattr(cfg, "DEFAULT_CONFIG_CANDIDATES",
                        (str(tmp_path / "missing-mcp.json"), str(own)))
    assert _resolve_config_path() == str(own)


def test_resolver_prefers_mcp_candidate_over_sndeck_candidate(tmp_path, monkeypatch):
    _clear(monkeypatch)
    import sndeck.config as cfg
    mcp = tmp_path / "mcp.json"; mcp.write_text("{}")
    own = tmp_path / "own.json"; own.write_text("{}")
    monkeypatch.setattr(cfg, "DEFAULT_CONFIG_CANDIDATES", (str(mcp), str(own)))
    assert _resolve_config_path() == str(mcp)


def test_resolver_returns_first_candidate_when_none_exist(tmp_path, monkeypatch):
    _clear(monkeypatch)
    import sndeck.config as cfg
    a = tmp_path / "missing-a.json"
    b = tmp_path / "missing-b.json"
    monkeypatch.setattr(cfg, "DEFAULT_CONFIG_CANDIDATES", (str(a), str(b)))
    assert _resolve_config_path() == str(a)

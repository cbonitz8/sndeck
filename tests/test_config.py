import json
from sndeck.config import load_instance


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

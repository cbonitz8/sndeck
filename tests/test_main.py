"""Entry-point behavior: help/usage and headless-safe TUI guard in sndeck.__main__."""
import sys
import pytest
import sndeck.__main__ as m


class _BoomApp:
    """Stand-in for SndeckApp that fails the test if the TUI is ever constructed."""
    def __init__(self, *a, **k):
        raise AssertionError("SndeckApp must not be constructed on this path")


@pytest.fixture
def no_tui(monkeypatch):
    monkeypatch.setattr(m, "SndeckApp", _BoomApp)


@pytest.mark.parametrize("flag", ["--help", "-h", "help"])
def test_help_flag_prints_usage_not_tui(flag, monkeypatch, capsys, no_tui):
    monkeypatch.setattr(sys, "argv", ["sndeck", flag])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Usage:" in out and "sndeck us" in out


def test_bare_headless_prints_usage_not_tui(monkeypatch, capsys, no_tui):
    monkeypatch.setattr(sys, "argv", ["sndeck"])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    rc = m.main()
    assert rc == 0
    assert "Usage:" in capsys.readouterr().out


def test_dir_arg_headless_prints_usage_not_tui(monkeypatch, capsys, no_tui):
    # A scratch-dir arg still launches the TUI on a terminal — but headless it must not hang.
    monkeypatch.setattr(sys, "argv", ["sndeck", "/tmp/somedir"])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    rc = m.main()
    assert rc == 0
    assert "Usage:" in capsys.readouterr().out


def test_bare_on_terminal_launches_tui(monkeypatch):
    ran = {}

    class _FakeApp:
        def __init__(self, *a, **k): ran["init"] = True
        def run(self): ran["run"] = True

    monkeypatch.setattr(sys, "argv", ["sndeck"])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(m, "load_instance", lambda name: object())
    monkeypatch.setattr(m, "TokenProvider", lambda inst: object())
    monkeypatch.setattr(m, "TableClient", lambda inst, tok: object())
    monkeypatch.setattr(m, "SndeckApp", _FakeApp)
    rc = m.main()
    assert rc == 0
    assert ran.get("run") is True


def test_subcommand_routes_to_cli(monkeypatch, no_tui):
    called = {}
    import sndeck.cli as cli

    def fake_dispatch(argv):
        called["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "dispatch", fake_dispatch)
    monkeypatch.setattr(sys, "argv", ["sndeck", "us", "get"])
    rc = m.main()
    assert rc == 0
    assert called["argv"] == ["us", "get"]


def test_terminal_launch_refuses_legacy_root(tmp_path, monkeypatch, capsys, no_tui):
    root = tmp_path / "vendor-hotfix"
    (root / ("rel__" + "b" * 32)).mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", ["sndeck", str(root)])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rc = m.main()
    err = capsys.readouterr().err
    assert rc == 2
    assert "legacy per-task root" in err and "(store root from argument)" in err


def test_terminal_launch_passes_store_root_to_tui(tmp_path, monkeypatch):
    seen = {}

    class _FakeApp:
        def __init__(self, client, root, theme): seen["root"] = root
        def run(self): pass

    monkeypatch.setattr(sys, "argv", ["sndeck", str(tmp_path)])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(m, "load_instance", lambda name: object())
    monkeypatch.setattr(m, "TokenProvider", lambda inst: object())
    monkeypatch.setattr(m, "TableClient", lambda inst, tok: object())
    monkeypatch.setattr(m, "SndeckApp", _FakeApp)
    assert m.main() == 0
    assert seen["root"] == str(tmp_path)

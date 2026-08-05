from sndeck.watcher import ScratchChanged, watch_scratch


def test_scratch_changed_carries_paths():
    m = ScratchChanged({"/a", "/b"})
    assert m.paths == {"/a", "/b"}


def test_watch_scratch_noops_without_watchfiles(monkeypatch, tmp_path):
    # Simulate watchfiles missing: patch the import symbol to None
    import sndeck.watcher as w
    monkeypatch.setattr(w, "watch", None)
    posted = []
    class FakeApp:
        def call_from_thread(self, *a, **k): posted.append(a)
    # should return immediately, posting nothing
    watch_scratch(FakeApp(), str(tmp_path), stop=lambda: True)
    assert posted == []

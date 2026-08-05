import json
from sndeck import state as st


def test_load_defaults_when_absent(tmp_path):
    p = str(tmp_path / "nope.json")
    s = st.load_state(p)
    assert s.tracked_sets == [] and s.split_ratio == st.DEFAULT_RATIO


def test_save_then_load_roundtrip(tmp_path):
    p = str(tmp_path / "state.json")
    st.save_state(st.State(["A", "B"], 0.55), p)
    s = st.load_state(p)
    assert s.tracked_sets == ["A", "B"] and s.split_ratio == 0.55


def test_pin_unpin(tmp_path):
    p = str(tmp_path / "state.json")
    st.pin("A", p); st.pin("A", p); st.pin("B", p)
    assert st.load_state(p).tracked_sets == ["A", "B"]
    st.unpin("A", p)
    assert st.load_state(p).tracked_sets == ["B"]


def test_set_split_ratio_preserves_pins(tmp_path):
    p = str(tmp_path / "state.json")
    st.pin("A", p)
    st.set_split_ratio(0.7, p)
    s = st.load_state(p)
    assert s.tracked_sets == ["A"] and s.split_ratio == 0.7


def test_malformed_file_returns_defaults(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    s = st.load_state(str(p))
    assert s.tracked_sets == [] and s.split_ratio == st.DEFAULT_RATIO


def test_env_override(tmp_path, monkeypatch):
    p = tmp_path / "custom.json"
    monkeypatch.setenv("SNDECK_STATE", str(p))
    st.pin("Z")
    assert json.loads(p.read_text())["tracked_sets"] == ["Z"]


def test_pin_names_roundtrip_and_backward_compat(tmp_path):
    p = str(tmp_path / "state.json")
    # old-format file with no pin_names key loads clean
    (tmp_path / "state.json").write_text('{"tracked_sets": ["A"], "split_ratio": 0.4}')
    s = st.load_state(p)
    assert s.pin_names == {}
    # save + reload preserves pin_names
    s.pin_names["A"] = "Set A"
    st.save_state(s, p)
    assert st.load_state(p).pin_names == {"A": "Set A"}


def test_pin_records_name_keyword_only(tmp_path):
    p = str(tmp_path / "state.json")
    st.pin("A", p, name="Set A")          # path positional, name keyword
    s = st.load_state(p)
    assert s.tracked_sets == ["A"] and s.pin_names == {"A": "Set A"}


def test_unpin_clears_name(tmp_path):
    p = str(tmp_path / "state.json")
    st.pin("A", p, name="Set A")
    st.unpin("A", p)
    s = st.load_state(p)
    assert s.tracked_sets == [] and s.pin_names == {}


def test_remember_pin_names_only_for_tracked(tmp_path):
    p = str(tmp_path / "state.json")
    st.pin("A", p)
    st.remember_pin_names({"A": "Set A", "Z": "Not tracked"}, p)
    s = st.load_state(p)
    assert s.pin_names == {"A": "Set A"}   # Z ignored (not tracked)

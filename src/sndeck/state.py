"""Mutable local state (~/.config/sndeck/state.json): pinned update sets + split ratio.
Separate from the read-only TOML config. SNDECK_STATE overrides the path."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

DEFAULT_STATE_PATH = "~/.config/sndeck/state.json"
DEFAULT_RATIO = 0.4


def _path(path: str | None = None) -> str:
    return os.path.expanduser(path or os.environ.get("SNDECK_STATE") or DEFAULT_STATE_PATH)


@dataclass
class State:
    tracked_sets: list[str]
    split_ratio: float = DEFAULT_RATIO
    pin_names: dict[str, str] = field(default_factory=dict)


def load_state(path: str | None = None) -> State:
    try:
        with open(_path(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return State([], DEFAULT_RATIO)
    ts = data.get("tracked_sets", [])
    if not isinstance(ts, list):
        ts = []
    try:
        ratio = float(data.get("split_ratio", DEFAULT_RATIO))
    except (TypeError, ValueError):
        ratio = DEFAULT_RATIO
    pn = data.get("pin_names", {})
    if not isinstance(pn, dict):
        pn = {}
    return State([str(x) for x in ts], ratio,
                 {str(k): str(v) for k, v in pn.items()})


def save_state(state: State, path: str | None = None) -> None:
    p = _path(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"tracked_sets": state.tracked_sets,
                   "split_ratio": state.split_ratio,
                   "pin_names": state.pin_names}, fh, indent=2)


def pin(sys_id: str, path: str | None = None, *, name: str | None = None) -> None:
    s = load_state(path)
    changed = False
    if sys_id not in s.tracked_sets:
        s.tracked_sets.append(sys_id)
        changed = True
    if name and s.pin_names.get(sys_id) != name:
        s.pin_names[sys_id] = name
        changed = True
    if changed:
        save_state(s, path)


def unpin(sys_id: str, path: str | None = None) -> None:
    s = load_state(path)
    changed = False
    if sys_id in s.tracked_sets:
        s.tracked_sets.remove(sys_id)
        changed = True
    if sys_id in s.pin_names:
        del s.pin_names[sys_id]
        changed = True
    if changed:
        save_state(s, path)


def remember_pin_names(names: dict[str, str], path: str | None = None) -> None:
    """Merge freshly-resolved set names into pin_names, for tracked sets only."""
    s = load_state(path)
    changed = False
    for sid, nm in names.items():
        if sid in s.tracked_sets and nm and s.pin_names.get(sid) != nm:
            s.pin_names[sid] = nm
            changed = True
    if changed:
        save_state(s, path)


def set_split_ratio(ratio: float, path: str | None = None) -> None:
    s = load_state(path)
    s.split_ratio = ratio
    save_state(s, path)

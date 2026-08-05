"""Live file-watching over the scratch dir. Degrades gracefully if watchfiles is absent."""
from __future__ import annotations

import os

from textual.message import Message

try:
    from watchfiles import watch  # type: ignore
except ImportError:  # optional dep / import failure
    watch = None  # type: ignore


class ScratchChanged(Message):
    def __init__(self, paths: set[str]) -> None:
        super().__init__()
        self.paths = paths


def watch_scratch(app, scratch_dir: str, stop) -> None:
    """Thread body: block on watchfiles.watch and post ScratchChanged batches.

    `stop` is a callable returning True when watching should end.
    No-ops if watchfiles is unavailable or the directory doesn't exist.
    """
    if watch is None or not os.path.isdir(scratch_dir):
        return
    for changes in watch(scratch_dir, stop_event=None, yield_on_timeout=True,
                         rust_timeout=200):
        if stop():
            return
        if changes:
            app.call_from_thread(app.post_message, ScratchChanged({p for _, p in changes}))

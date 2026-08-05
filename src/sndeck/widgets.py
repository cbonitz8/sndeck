"""Custom widgets for sndeck: the draggable split divider."""
from __future__ import annotations

from textual.widgets import Static


class SplitHandle(Static):
    """A 1-row divider between the tree and preview. Drag to resize."""

    def __init__(self, **kwargs) -> None:
        super().__init__("─" * 200, **kwargs)
        self._dragging: bool = False

    def on_mouse_down(self, event) -> None:
        self._dragging = True
        self.capture_mouse()
        event.stop()

    def on_mouse_up(self, event) -> None:
        self._dragging = False
        self.release_mouse()
        self.app._persist_ratio()  # type: ignore[attr-defined]
        event.stop()

    def on_mouse_move(self, event) -> None:
        if self._dragging:
            split = self.app.query_one("#split")
            # pointer row relative to the split container
            rel_y = event.screen_y - split.region.y
            ratio = rel_y / max(split.region.height, 1)
            self.app._apply_ratio(ratio)  # type: ignore[attr-defined]
            event.stop()

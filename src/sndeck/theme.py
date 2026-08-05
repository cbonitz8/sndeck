"""sndeck Textual theme definitions: Catppuccin Macchiato (dark) and Latte (light)."""
from __future__ import annotations

from textual.theme import Theme

MACCHIATO = "catppuccin-macchiato"
LATTE = "catppuccin-latte"

_macchiato = Theme(
    name=MACCHIATO,
    dark=True,
    background="#24273a",
    surface="#363a4f",
    panel="#1e2030",
    primary="#8aadf4",
    accent="#8aadf4",
    secondary="#b7bdf8",
    foreground="#cad3f5",
    success="#a6da95",
    warning="#eed49f",
    error="#ed8796",
    variables={
        "crust": "#181926",
        "subtext": "#6e738d",
        "label": "#7480a2",
        "row-hover": "#2d3153",
        "surface-selected": "#363a4f",
    },
)

_latte = Theme(
    name=LATTE,
    dark=False,
    background="#eff1f5",
    surface="#ccd0da",
    panel="#e6e9ef",
    primary="#1e66f5",
    accent="#1e66f5",
    secondary="#7287fd",
    foreground="#4c4f69",
    success="#40a02b",
    warning="#df8e1d",
    error="#d20f39",
    variables={
        "crust": "#dce0e8",
        "subtext": "#6c6f85",
        "label": "#6c6f85",
        "row-hover": "#dce0e8",
        "surface-selected": "#bcc0cc",
    },
)

THEMES: list[Theme] = [_macchiato, _latte]


def next_theme(name: str) -> str:
    """Toggle between the two sndeck themes."""
    return LATTE if name == MACCHIATO else MACCHIATO

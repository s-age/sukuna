"""Which direction a new pane is split from its anchor."""

from __future__ import annotations

from enum import StrEnum


class SplitDirection(StrEnum):
    HORIZONTAL = "horizontal"  # children arranged side by side (left/right); adopts tmux's own vocabulary as-is
    VERTICAL = "vertical"  # children stacked top/bottom

from __future__ import annotations

MIN_SAFE_PANE_WIDTH = 10
"""Columns (character-grid width). Safety policy: never squeeze another
pane below this threshold. `iterm_script.py` runs in iTerm2's own Python
plugin environment and cannot import the sukuna package, so it duplicates
this same value on its own as a "deliberate mirror" (`_MIN_SAFE_PANE_WIDTH`)
-- tests/test_pane_width_parity.py detects drift between the two effective
results."""


def compute_clamped_percent(
    *,
    window_width: int,
    other_pane_count: int,
    percent: int,
    min_safe_width: int = MIN_SAFE_PANE_WIDTH,
) -> tuple[int, str | None]:
    """Clamp arithmetic (a pure function) applied before passing a value to
    `resize-pane -x <percent>%`. Resolving pane_ref/window_ref and the
    actual tmux queries (I/O) stay with the caller (infrastructure layer).

    A window-wide `resize-pane -x` applied to other panes outside this
    function can steal columns even from panes with no direct
    parent/child relationship to `pane_ref`. Since tmux offers no dry run,
    this is a conservative upper bound that assumes every other pane could
    be squeezed down to `min_safe_width` (it can over-clamp when
    `pane_ref` is not adjacent to all of them). Not an absolute guarantee:
    in a degenerate case where the window itself is narrower than
    `other_pane_count * min_safe_width` plus `pane_ref`'s own share (e.g.
    a 15-column window with 2 other panes), `safe_width` floors at
    `min_safe_width`, and a `percent` that already fits within that floor
    passes through without a warning -- the other panes still end up
    squeezed below `min_safe_width`.

    Handles the same formula as iterm_script.py's `compute_clamped_width`,
    but with a different output unit (percent here, width there) --
    tests/test_pane_width_parity.py verifies the effective widths match.
    """
    if other_pane_count <= 0:
        return percent, None
    max_safe_width = window_width - other_pane_count * min_safe_width
    requested_width = round(window_width * percent / 100)
    if max_safe_width >= min_safe_width and requested_width <= max_safe_width:
        return percent, None
    safe_width = max(min_safe_width, max_safe_width)
    clamped_percent = max(1, min(percent, (safe_width * 100) // window_width))
    if clamped_percent == percent:
        return percent, None
    warning = (
        f"requested active_pane_width={percent}% of window width {window_width} "
        f"would squeeze {other_pane_count} other pane(s) below "
        f"{min_safe_width} columns; clamped to {clamped_percent}%"
    )
    return clamped_percent, warning

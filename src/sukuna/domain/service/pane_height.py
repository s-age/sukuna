from __future__ import annotations


def equalize_heights(heights: list[int]) -> int:
    """Column-equalization target height = sum of current heights / member
    count (floor division). I/O and the application loop (apply to the
    first N-1 members, the last absorbs the remainder) stay with the
    caller (`infrastructure.terminal.operations`). `iterm_script.py`
    cannot import the sukuna package, so it duplicates the same formula as
    `compute_equalize_target` -- tests/test_equalize_parity.py detects
    drift."""
    return sum(heights) // len(heights)

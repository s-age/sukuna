"""Parity between `domain.service.pane_height.equalize_heights` (the
equalize target-height formula) and `iterm_script.compute_equalize_target`
(iterm_script.py's independent, intentionally-kept duplicate of the same
policy -- that file runs inside iTerm2's Python plugin environment and
cannot import the sukuna package, same constraint and same solution as
`tests/test_pane_width_parity.py`).

Unlike the pane-width pair, the two functions here share one unit (rows)
and one output (the target height), so the assertion is direct equality
over the same inputs."""

from itertools import product

from sukuna.domain.service.pane_height import equalize_heights
from sukuna.infrastructure.terminal.iterm_script import compute_equalize_target


def test_target_height_agrees_across_an_exhaustive_two_member_sweep() -> None:
    mismatches = [
        (heights, equalize_heights(list(heights)))
        for heights in product(range(120), repeat=2)
        if equalize_heights(list(heights)) != compute_equalize_target(list(heights))
    ]

    assert not mismatches, f"equalize target parity broke for: {mismatches[:10]}"


def test_target_height_agrees_across_a_broad_sweep_of_longer_columns() -> None:
    mismatches = []
    for length in (3, 4, 5):
        for heights in product(range(0, 61, 7), repeat=length):
            domain_target = equalize_heights(list(heights))
            iterm_target = compute_equalize_target(list(heights))
            if domain_target != iterm_target:
                mismatches.append((heights, domain_target, iterm_target))

    assert not mismatches, f"equalize target parity broke for: {mismatches[:10]}"

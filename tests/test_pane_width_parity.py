"""Parity between `domain.service.pane_width.compute_clamped_percent`
(the clamping algorithm) and `iterm_script.compute_clamped_width`
(iterm_script.py's independent, intentionally-kept duplicate of the same
policy).

Only the applied *width* is asserted to always agree -- the two functions
return different units (percent vs. width) by design (F15), so a percent
from one side is converted to a width via `round(window_width * percent /
100)` before comparing. `_MIN_SAFE_PANE_WIDTH` is asserted equal separately
so a drift in the threshold constant itself is caught even though nothing
above depends on reading it directly.

Warning *presence* is deliberately NOT asserted to always agree: the two
implementations fire on different conditions (tmux: `clamped_percent !=
percent`; iterm: `applied_width != requested_width`, documented in
iterm_script.py's `compute_clamped_width` docstring) and can diverge at a
rounding boundary even when the applied width matches. That divergence is
a known, accepted low-severity NIT --
`test_warning_presence_can_diverge_at_a_known_rounding_boundary`
below pins the concrete boundary case so a future change that removes the
divergence (or changes its shape) is visible instead of silent.
"""

from sukuna.domain.service.pane_width import (
    MIN_SAFE_PANE_WIDTH,
    compute_clamped_percent,
)
from sukuna.infrastructure.terminal.iterm_script import (
    _MIN_SAFE_PANE_WIDTH as ITERM_MIN_SAFE_PANE_WIDTH,
)
from sukuna.infrastructure.terminal.iterm_script import compute_clamped_width


def _applied_width_via_domain(
    *, window_width: int, other_pane_count: int, percent: int
) -> int:
    clamped_percent, _ = compute_clamped_percent(
        window_width=window_width, other_pane_count=other_pane_count, percent=percent
    )
    return round(window_width * clamped_percent / 100)


def _applied_width_via_iterm(
    *, window_width: int, other_pane_count: int, percent: int
) -> int:
    applied_width, _ = compute_clamped_width(
        total_width=window_width,
        other_slot_widths=[0] * other_pane_count,
        percent=percent,
    )
    return applied_width


def test_the_two_implementations_share_the_same_threshold_constant() -> None:
    assert MIN_SAFE_PANE_WIDTH == ITERM_MIN_SAFE_PANE_WIDTH


def test_applied_width_agrees_across_a_broad_sweep_of_inputs() -> None:
    """Swept exhaustively (not sampled) over a range wide enough to cover
    every branch of both functions: no-siblings, within-bounds,
    over-the-floor clamp, and the degenerate narrow-window floor case."""
    mismatches = []
    for window_width in range(1, 250):
        for other_pane_count in range(6):
            for percent in range(1, 100):
                domain_width = _applied_width_via_domain(
                    window_width=window_width,
                    other_pane_count=other_pane_count,
                    percent=percent,
                )
                iterm_width = _applied_width_via_iterm(
                    window_width=window_width,
                    other_pane_count=other_pane_count,
                    percent=percent,
                )
                if domain_width != iterm_width:
                    mismatches.append(
                        (
                            window_width,
                            other_pane_count,
                            percent,
                            domain_width,
                            iterm_width,
                        )
                    )

    assert not mismatches, f"width parity broke for: {mismatches[:10]}"


def test_warning_presence_can_diverge_at_a_known_rounding_boundary() -> None:
    """Pinned regression case for the documented NIT: at window_width=11,
    other_pane_count=1, percent=91, both sides clamp to the same applied
    width (10), but tmux's `clamped_percent(90) != percent(91)` still fires
    a warning while iterm's `applied_width(10) == requested_width(round(11
    * 91 / 100) == 10)` does not."""
    domain_percent, domain_warning = compute_clamped_percent(
        window_width=11, other_pane_count=1, percent=91
    )
    iterm_width, iterm_warning = compute_clamped_width(
        total_width=11, other_slot_widths=[0], percent=91
    )

    assert round(11 * domain_percent / 100) == iterm_width
    assert domain_warning is not None
    assert iterm_warning is None

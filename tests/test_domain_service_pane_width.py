from sukuna.domain.service.pane_width import (
    MIN_SAFE_PANE_WIDTH,
    compute_clamped_percent,
)


def test_no_other_panes_returns_the_requested_percent_unclamped() -> None:
    percent, warning = compute_clamped_percent(
        window_width=212, other_pane_count=0, percent=50
    )

    assert percent == 50
    assert warning is None


def test_within_safe_bounds_is_not_clamped() -> None:
    percent, warning = compute_clamped_percent(
        window_width=200, other_pane_count=1, percent=50
    )

    assert percent == 50
    assert warning is None


def test_squeezing_a_sibling_below_the_floor_is_clamped_with_a_warning() -> None:
    percent, warning = compute_clamped_percent(
        window_width=100, other_pane_count=1, percent=95
    )

    assert percent < 95
    assert warning is not None
    assert "clamped" in warning


def test_multiple_siblings_each_reserve_the_floor() -> None:
    percent, warning = compute_clamped_percent(
        window_width=100, other_pane_count=3, percent=90
    )

    applied_width = round(100 * percent / 100)
    assert applied_width <= 100 - 3 * MIN_SAFE_PANE_WIDTH
    assert warning is not None


def test_degenerate_narrow_window_floors_without_a_warning() -> None:
    """Known theoretical gap: a window narrower than `other_pane_count *
    min_safe_width` plus a usable share floors `safe_width` at
    `min_safe_width`, and a `percent` whose ratio already sits at or below
    that floor passes through unclamped -- even though the other panes
    still end up squeezed below `min_safe_width`."""
    percent, warning = compute_clamped_percent(
        window_width=15, other_pane_count=2, percent=10
    )

    assert percent == 10
    assert warning is None

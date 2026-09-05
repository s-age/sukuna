from sukuna.domain.service.resize_convergence import (
    RESIZE_TOLERANCE,
    build_unconverged_warning,
    next_convergence_percent,
)


def test_within_tolerance_is_converged() -> None:
    assert next_convergence_percent(requested=70, achieved=68) is None


def test_exactly_at_the_tolerance_boundary_is_converged() -> None:
    assert (
        next_convergence_percent(requested=70, achieved=70 + RESIZE_TOLERANCE) is None
    )


def test_just_past_the_tolerance_boundary_overshoots() -> None:
    # requested=70, achieved=74 (diff=4) -> 70 - 4 = 66
    assert (
        next_convergence_percent(requested=70, achieved=70 + RESIZE_TOLERANCE + 1) == 66
    )


def test_overshoot_beyond_tolerance_corrects_downward_by_the_sign_flipped_delta() -> (
    None
):
    # requested=70, achieved=80 (diff=10) -> 70 - (80-70) = 60
    assert next_convergence_percent(requested=70, achieved=80) == 60


def test_undershoot_keeps_retrying_the_original_requested_percent() -> None:
    assert next_convergence_percent(requested=70, achieved=40) == 70
    assert next_convergence_percent(requested=70, achieved=55) == 70


def test_extreme_overshoot_correction_is_clamped_to_0() -> None:
    # requested=10, achieved=95 -> 10 - (95-10) = -75, clamped to 0
    assert next_convergence_percent(requested=10, achieved=95) == 0


def test_overshoot_correction_above_100_is_clamped_to_100() -> None:
    # requested=200, achieved=250 (diff=50) -> 200 - 50 = 150, clamped to
    # 100. The function itself doesn't validate its inputs stay within the
    # real 0-100 percent domain, so this exercises the upper clamp in
    # isolation the same way the card's max(0, min(100, ...)) formula does.
    assert next_convergence_percent(requested=200, achieved=250) == 100


def test_build_unconverged_warning_reports_requested_achieved_and_attempt_count() -> (
    None
):
    warning = build_unconverged_warning(requested=70, achieved=10, max_attempts=6)

    assert "70%" in warning
    assert "10%" in warning
    assert "6" in warning

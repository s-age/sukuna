from __future__ import annotations

RESIZE_TOLERANCE = 3
MAX_RESIZE_ATTEMPTS = 6


def next_convergence_percent(*, requested: int, achieved: int) -> int | None:
    """`None` means `achieved` is already within `RESIZE_TOLERANCE` of
    `requested` (converged, stop retrying). On overshoot, the next attempt
    asks for a sign-flipped correction (`requested - (achieved -
    requested)`, clamped to `[0, 100]`); undershoot keeps retrying the
    original `requested` unchanged."""
    if abs(achieved - requested) <= RESIZE_TOLERANCE:
        return None
    if achieved > requested + RESIZE_TOLERANCE:
        return max(0, min(100, requested - (achieved - requested)))
    return requested


def build_unconverged_warning(
    *, requested: int, achieved: int, max_attempts: int
) -> str:
    """Warning text for when the retry loop exhausts `max_attempts` without
    ever landing within `RESIZE_TOLERANCE` of `requested`."""
    return f"requested active_pane_width={requested}%, achieved {achieved}% after {max_attempts} attempts"

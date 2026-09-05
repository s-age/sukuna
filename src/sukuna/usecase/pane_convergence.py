"""Shared active_pane_width convergence retry, used by both `spawn` (iTerm2
post-split percent application) and `resize` (`sukuna resize`, self-resize
hook)."""

from __future__ import annotations

from ..domain.mapper.terminal_mapper import ResizeResponse
from ..domain.service.resize_convergence import (
    MAX_RESIZE_ATTEMPTS,
    build_unconverged_warning,
    next_convergence_percent,
)
from ..infrastructure.terminal import operations as terminal_ops


def converge_active_pane_width(
    *, backend: str, pane_ref: str, percent: int
) -> tuple[ResizeResponse, str | None]:
    """Retries a bounded number of times, reading back the actual achieved
    occupancy after each attempt, until within tolerance. Returns
    `(last_response, warning)`: the final attempt's raw `ResizeResponse`,
    and a warning if the loop never converged (`None` if it converged or
    the backend reported no achieved percent)."""
    last_response: ResizeResponse | None = None
    last_achieved: int | None = None
    next_percent = percent
    for _ in range(1, MAX_RESIZE_ATTEMPTS + 1):
        last_response = terminal_ops.resize(
            backend=backend, pane_ref=pane_ref, percent=next_percent
        )
        last_achieved = last_response.achieved_percent
        if last_achieved is None:
            return last_response, None
        decision = next_convergence_percent(requested=percent, achieved=last_achieved)
        if decision is None:
            return last_response, None
        next_percent = decision
    assert (
        last_response is not None and last_achieved is not None
    )  # loop always runs >=1 attempt (MAX_RESIZE_ATTEMPTS > 0); a None
    # achieved_percent returns early above, so falling through here means
    # the last iteration's readback was an int.
    return (
        last_response,
        build_unconverged_warning(
            requested=percent, achieved=last_achieved, max_attempts=MAX_RESIZE_ATTEMPTS
        ),
    )

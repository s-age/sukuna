from pathlib import Path

import pytest
from _terminal_fakes import make_worker as _make_worker

from sukuna.domain.entity.worker_record import (
    TERMINAL_STATES,
    WorkerRecord,
    WorkerState,
)
from sukuna.errors import StateError


def _worker(
    *, state: WorkerState, pane_ref: str | None, managed: bool = True
) -> WorkerRecord:
    return _make_worker(
        Path("/repo"),
        name="ccw-project-00000000-review-1",
        pane_ref=pane_ref,
        managed=managed,
        worker_state=state,
    )


def test_terminal_states_contents_are_explicit() -> None:
    """Pin `TERMINAL_STATES`'s contents so a future edit can't silently
    shrink/grow it again."""
    assert TERMINAL_STATES == frozenset(
        {WorkerState.FAILED, WorkerState.TIMED_OUT, WorkerState.CLOSED}
    )


@pytest.mark.parametrize(
    "state", [WorkerState.CLOSED, WorkerState.FAILED, WorkerState.TIMED_OUT]
)
def test_terminal_states_now_allow_a_transition_to_starting(state: WorkerState) -> None:
    worker = _worker(state=state, pane_ref=None)

    worker.transition_to(WorkerState.STARTING)

    assert worker.state is WorkerState.STARTING


@pytest.mark.parametrize(
    "state",
    [
        WorkerState.STARTING,
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
    ],
)
def test_non_terminal_states_still_reject_a_transition_to_starting(
    state: WorkerState,
) -> None:
    worker = _worker(state=state, pane_ref=None)

    with pytest.raises(StateError, match="cannot transition"):
        worker.transition_to(WorkerState.STARTING)


@pytest.mark.parametrize(
    "state", [WorkerState.CLOSED, WorkerState.FAILED, WorkerState.TIMED_OUT]
)
def test_may_respawn_true_for_a_managed_terminal_worker_with_no_pane(
    state: WorkerState,
) -> None:
    assert _worker(state=state, pane_ref=None).may_respawn is True


@pytest.mark.parametrize(
    "state", [WorkerState.CLOSED, WorkerState.FAILED, WorkerState.TIMED_OUT]
)
def test_may_respawn_false_when_a_pane_is_still_attached(state: WorkerState) -> None:
    assert _worker(state=state, pane_ref="%1").may_respawn is False


@pytest.mark.parametrize(
    "state",
    [
        WorkerState.STARTING,
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
    ],
)
def test_may_respawn_false_for_a_non_terminal_state(state: WorkerState) -> None:
    assert _worker(state=state, pane_ref=None).may_respawn is False


def test_may_respawn_false_for_an_unmanaged_worker() -> None:
    worker = _worker(state=WorkerState.CLOSED, pane_ref=None, managed=False)

    assert worker.may_respawn is False

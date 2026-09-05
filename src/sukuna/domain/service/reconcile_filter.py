from __future__ import annotations

from ..entity.worker_record import TERMINAL_STATES, WorkerRecord, WorkerState

_STALE_PANE_STATES = TERMINAL_STATES - {WorkerState.CLOSED}


def reconcilable_workers(workers: list[WorkerRecord]) -> list[WorkerRecord]:
    """Only a managed worker with a live `pane_ref` and a non-terminal
    state is a reconciliation candidate. `pane_ref is None` means nothing
    to verify against. A worker already in `TERMINAL_STATES` has no
    outgoing transition. An unmanaged worker (`managed=False`) is outside
    sukuna's lifecycle entirely."""
    return [
        worker
        for worker in workers
        if worker.managed
        and worker.pane_ref is not None
        and worker.state not in TERMINAL_STATES
    ]


def may_manually_fail(worker: WorkerRecord, *, pane_confirmed_gone: bool) -> bool:
    """Manual `sukuna state --state failed` (`usecase/state.py`) is a
    recovery path for a record already known to lack a live pane, not a
    way to force a worker whose pane is still running into FAILED. Permits
    the transition when there is nothing to verify (`pane_ref is None`) or
    a verify call already confirmed the pane is gone; the caller folds
    "verification itself failed" (e.g. a `CrossBufferError`) into
    `pane_confirmed_gone=False`."""
    return worker.pane_ref is None or pane_confirmed_gone


def should_warn_manual_timed_out(
    worker: WorkerRecord, *, pane_confirmed_alive: bool
) -> bool:
    """Manual `sukuna state --state timed_out` (`usecase/state.py`) has no
    verify-and-reject guard like `may_manually_fail()`'s -- the transition
    always succeeds; this predicate only decides whether the result
    carries a warning that the pane still verifies as alive. Warns only
    when a verify call positively confirmed the pane is alive; the caller
    folds "nothing to verify" (`pane_ref is None`) and "verification
    failed" into `pane_confirmed_alive=False`."""
    return worker.pane_ref is not None and pane_confirmed_alive


def stale_pane_candidates(workers: list[WorkerRecord]) -> list[WorkerRecord]:
    """A managed worker at FAILED/TIMED_OUT (CLOSED excluded) with a live
    `pane_ref` is not a `reconcilable_workers()` candidate, but that
    `pane_ref` can still be stale (destroyed out-of-band after the worker
    reached a terminal state). Left uncleared, this permanently blocks a
    parent's `close()` via `alive_pane_holders()`'s live-child guard."""
    return [
        worker
        for worker in workers
        if worker.managed
        and worker.pane_ref is not None
        and worker.state in _STALE_PANE_STATES
    ]

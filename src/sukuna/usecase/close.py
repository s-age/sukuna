"""Close an accepted managed worker's pane."""

from __future__ import annotations

from typing import Any

from ..domain.entity.worker_record import WorkerRecord, WorkerState
from ..domain.mapper.registry_mapper import record_to_dict
from ..domain.mapper.terminal_mapper import CloseResponse
from ..domain.service.spawn_placement import (
    alive_pane_holders,
    blocking_children_names,
    equalize_target,
    orphaned_and_column,
    orphaned_children_message,
)
from ..errors import (
    BackendError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from ..infrastructure.registry import Registry
from ..infrastructure.terminal import operations as terminal_ops
from ..infrastructure.terminal.resolver import infer_backend
from .spawn_shared import reraise_after_best_effort
from .window_frame import preserve_window_frame


def _mark_closed(current: WorkerRecord) -> None:
    current.transition_to(WorkerState.CLOSED)
    # The destructive `terminal_ops.close()` call above has already
    # destroyed the pane by the time this mutation runs, so the CLOSED
    # record must not keep pointing at it. Unlike the ConflictError path
    # below and reconcile's pane-vanish handling (both of which only
    # detach `pane_ref`), this success path also clears `window_ref` --
    # the record is CLOSED here, so nothing else can still need the window
    # (see `WorkerRecord.detach_pane()` for the full policy).
    current.detach_pane(clear_window=True)


def _equalize_fields(column: list[WorkerRecord]) -> dict[str, str]:
    refs = equalize_target(column)
    if not refs or len(refs) < 2:
        return {}
    try:
        terminal_ops.equalize(backend=infer_backend(refs[0]), column_pane_refs=refs)
    except BackendError as error:
        return {"equalize_warning": str(error)}
    return {}


def _build_close_result(
    registry: Registry,
    worker: WorkerRecord,
    closed: WorkerRecord,
    close_response: CloseResponse,
) -> dict[str, Any]:
    """Assemble the close result: orphan detection, warnings, and the
    equalize follow-up."""
    orphaned, column = orphaned_and_column(registry.list(), worker)

    result = record_to_dict(closed)
    if close_response.close_warning is not None:
        result["close_warning"] = close_response.close_warning
    if orphaned:
        result["orphaned_children_warning"] = orphaned_children_message(orphaned)
    result |= _equalize_fields(column)
    return result


def close(registry: Registry, name: str) -> dict[str, Any]:
    # A single snapshot read (instead of `get()` then a separate `list()`)
    # removes the window between the two where a concurrent mutation could
    # be missed by the live-child guard below.
    snapshot = registry.list()
    worker = next((w for w in snapshot if w.name == name), None)
    if worker is None:
        raise NotFoundError(f"worker {name!r} is not registered")
    if not worker.may_close:
        raise ValidationError(
            "only managed workers in accepted state with a live pane may be closed"
        )

    blocked = blocking_children_names(alive_pane_holders(snapshot), worker.name)
    if blocked:
        raise ValidationError(
            f"cannot close {worker.name!r}: still has live child worker(s): {blocked}"
        )

    pane_ref = worker.pane_ref
    assert pane_ref is not None
    backend = infer_backend(pane_ref)
    # The frame is recorded before the destructive `terminal_ops.close()`
    # call below, while the pane still exists to resolve a window from, and
    # restored once this block exits -- spanning the ConflictError raise
    # path too: the pane is already destroyed by then regardless of which
    # exit this function takes.
    with preserve_window_frame(backend=backend, window_ref=worker.window_ref):
        close_response = terminal_ops.close(backend=backend, pane_ref=pane_ref)
        try:
            closed = registry.mutate(
                worker.name, _mark_closed, expected_updated_at=worker.updated_at
            )
        except ConflictError as original:
            reraise_after_best_effort(
                original,
                "detach the pane from the conflicting record",
                lambda: registry.mutate(
                    worker.name, lambda current: current.detach_pane()
                ),
            )

        return _build_close_result(registry, worker, closed, close_response)

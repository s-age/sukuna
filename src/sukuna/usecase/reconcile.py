"""Verify managed workers' panes still exist; fail the ones that don't."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..domain.entity.worker_record import WorkerRecord, WorkerState
from ..domain.mapper.registry_mapper import record_to_dict
from ..domain.service.reconcile_filter import (
    reconcilable_workers,
    stale_pane_candidates,
)
from ..errors import ConflictError, NotFoundError, StateError
from ..infrastructure.registry import Registry
from ..infrastructure.settings import load_retention_days
from .pane_verify import verify_pane_exists


def _fail_and_clear_pane(worker: WorkerRecord) -> None:
    worker.transition_to(WorkerState.FAILED)
    worker.detach_pane()


def _clear_pane_only(worker: WorkerRecord) -> None:
    worker.detach_pane()


def _verify_pane_gone(worker: WorkerRecord) -> bool:
    pane_ref = worker.pane_ref
    assert pane_ref is not None
    return verify_pane_exists(pane_ref) is False


def _fail_workers_with_a_gone_pane(
    registry: Registry, candidates: list[WorkerRecord]
) -> list[dict[str, Any]]:
    reconciled: list[dict[str, Any]] = []
    for worker in candidates:
        if not _verify_pane_gone(worker):
            continue
        try:
            updated = registry.mutate(
                worker.name,
                _fail_and_clear_pane,
                expected_updated_at=worker.updated_at,
            )
        except (StateError, ConflictError, NotFoundError):
            continue
        reconciled.append(record_to_dict(updated))
    return reconciled


def _clear_stale_panes(
    registry: Registry, candidates: list[WorkerRecord]
) -> list[dict[str, Any]]:
    pane_cleared: list[dict[str, Any]] = []
    for worker in candidates:
        if not _verify_pane_gone(worker):
            continue
        try:
            updated = registry.mutate(
                worker.name,
                _clear_pane_only,
                expected_updated_at=worker.updated_at,
            )
        except (ConflictError, NotFoundError):
            continue
        pane_cleared.append(record_to_dict(updated))
    return pane_cleared


def _prune_stale_records(
    registry: Registry, *, now: datetime | None
) -> list[dict[str, Any]]:
    """`reconcile --prune`'s purge call: always `force=True`."""
    purge_result = registry.purge(
        retention_days=load_retention_days(),
        now=now if now is not None else datetime.now(UTC),
        force=True,
    )
    return [record_to_dict(worker) for worker in purge_result.purged]


def reconcile(
    registry: Registry,
    *,
    prune: bool = False,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Verify every reconcilable worker's pane; FAIL the ones whose pane is
    gone. Also verify every terminal-state (FAILED/TIMED_OUT) worker still
    holding a `pane_ref`, clearing it (no transition) when that pane is
    gone too. Returns `{"reconciled": [...], "pane_cleared": [...]}` --
    the two are disjoint, since a worker transitioned by the first pass
    no longer has a `pane_ref` to match the second pass's filter -- plus
    a `"purged"` key when `prune=True` (retires CLOSED/FAILED(pane-less)
    records past `retention_days`, always with `force=True`).

    A `verify` call that itself fails (`CrossBufferError`) leaves that
    worker untouched in both passes. Both passes read one `registry.list()`
    snapshot; `expected_updated_at` detects concurrent mutation
    (`ConflictError`/`StateError`/`NotFoundError`) and skips that worker."""
    snapshot = registry.list()
    reconciled = _fail_workers_with_a_gone_pane(
        registry, reconcilable_workers(snapshot)
    )
    pane_cleared = _clear_stale_panes(registry, stale_pane_candidates(snapshot))

    result: dict[str, Any] = {"reconciled": reconciled, "pane_cleared": pane_cleared}
    if prune:
        result["purged"] = _prune_stale_records(registry, now=now)
    return result

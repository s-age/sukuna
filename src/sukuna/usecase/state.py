"""Apply a checked worker lifecycle transition."""

from __future__ import annotations

import os
from typing import Any

from ..domain.entity.worker_record import WorkerRecord, WorkerState
from ..domain.mapper.registry_mapper import record_to_dict
from ..domain.service.reconcile_filter import (
    may_manually_fail,
    should_warn_manual_timed_out,
)
from ..domain.service.spawn_placement import is_nested_caller_pane_state
from ..domain.service.worker_tree import build_tree, find_deepest_busy
from ..errors import CrossBufferError, ValidationError
from ..infrastructure.registry import Registry
from ..infrastructure.terminal.resolver import detect_backend, orchestrator_pane_ref
from .pane_verify import verify_pane_exists
from .resize import resize


def _dispatch_focus(registry: Registry, result: dict[str, Any]) -> None:
    """Follow the deepest BUSY worker's pane, or fall back to the caller's
    own pane once none remain. Applicability itself (backend resolution,
    "is the caller a nested worker" check) is skipped silently -- only a
    failure once dispatch has actually committed to a target surfaces as
    `focus_warning`."""
    try:
        backend = detect_backend()
        own_pane_ref = orchestrator_pane_ref(backend)
    except CrossBufferError:
        return
    if own_pane_ref is None:
        return

    workers = registry.list()
    is_nested_caller = any(
        record.pane_ref == own_pane_ref and is_nested_caller_pane_state(record.state)
        for record in workers
    )
    if is_nested_caller:
        return

    root_key = os.environ.get("CLAUDE_CODE_SESSION_ID") or "(unknown parent)"
    tree = build_tree(workers)
    try:
        target_name = find_deepest_busy(tree, root_key)
        resize(registry, target_name)
    except CrossBufferError as error:
        result["focus_warning"] = str(error)


def _guard_manual_fail(registry: Registry, name: str) -> WorkerRecord:
    worker = registry.get(name)
    pane_confirmed_gone = (
        verify_pane_exists(worker.pane_ref) is False
        if worker.pane_ref is not None
        else False
    )
    if not may_manually_fail(worker, pane_confirmed_gone=pane_confirmed_gone):
        raise ValidationError(
            f"cannot mark {name!r} failed: its pane ({worker.pane_ref!r}) still "
            "verifies as alive or could not be verified; run 'sukuna reconcile' "
            "if the pane is truly gone, or accept it and then close it"
        )
    return worker


def _warn_manual_timed_out(worker: WorkerRecord, result: dict[str, Any]) -> None:
    pane_confirmed_alive = (
        verify_pane_exists(worker.pane_ref) is True
        if worker.pane_ref is not None
        else False
    )
    if should_warn_manual_timed_out(worker, pane_confirmed_alive=pane_confirmed_alive):
        result["pane_alive_warning"] = (
            f"{worker.name!r} was marked timed_out but its pane "
            f"({worker.pane_ref!r}) still verifies as alive; if it is a "
            "nested orchestrator it may keep dispatching focus to its own "
            "subtree"
        )


def state(registry: Registry, worker: str, target_state: str) -> dict[str, Any]:
    try:
        target = WorkerState(target_state)
    except ValueError as error:
        raise ValidationError(f"invalid state {target_state!r}") from error
    if target is WorkerState.CLOSED:
        raise ValidationError("close a worker with 'sukuna close', not 'sukuna state'")
    if target is WorkerState.STARTING:
        raise ValidationError(
            "respawn a worker with 'sukuna respawn', not 'sukuna state'"
        )
    # The guard's read/verify and the transition below are two separate
    # registry accesses with a backend round-trip in between; carrying the
    # guarded snapshot's `updated_at` into the transition as a CAS closes
    # that window (same discipline as close/reconcile/respawn's
    # `expected_updated_at` usage). A concurrent writer (typically a
    # respawn re-attaching a live pane) surfaces as `ConflictError` instead
    # of silently recreating the live-pane_ref-FAILED record the manual-fail
    # guard exists to prevent. Non-FAILED targets keep
    # `expected_updated_at=None` (no guard, no check-then-act window).
    expected_updated_at: str | None = None
    if target is WorkerState.FAILED:
        expected_updated_at = _guard_manual_fail(registry, worker).updated_at
    record = registry.transition(
        worker, target, expected_updated_at=expected_updated_at
    )
    result = record_to_dict(record)
    if target in (WorkerState.BUSY, WorkerState.REPORTED):
        _dispatch_focus(registry, result)
    if target is WorkerState.TIMED_OUT:
        _warn_manual_timed_out(record, result)
    return result

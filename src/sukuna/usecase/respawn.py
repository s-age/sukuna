"""Reopen a pane-less terminal worker via `claude --resume`.

Mirrors `usecase/spawn.py`'s `_spawn_one()` contract (terminal -> STARTING ->
(READY | FAILED)) as closely as possible, reusing its shared helpers from
`usecase/spawn_shared.py` rather than re-deriving the same policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.entity.pane import SplitDirection
from ..domain.entity.worker_record import WorkerRecord, WorkerState
from ..domain.mapper.command_mapper import resume_command
from ..domain.mapper.registry_mapper import record_to_dict
from ..domain.mapper.terminal_mapper import SpawnResult
from ..domain.service.session_log import resolve_respawn_model
from ..errors import ConflictError, CrossBufferError, NotFoundError, ValidationError
from ..infrastructure.claude_projects import resolve_last_session_state
from ..infrastructure.filesystem import resolve_existing_directory
from ..infrastructure.registry import Registry
from ..infrastructure.settings import load_active_pane_width
from ..infrastructure.terminal import operations as terminal_ops
from ..infrastructure.terminal.resolver import (
    detect_backend,
    orchestrator_pane_ref,
    resolve_login_shell,
)
from .spawn_shared import (
    attach_spawn_warnings,
    finalize_spawned_pane,
    reject_cross_backend_continuation,
    reraise_after_best_effort,
    resolve_placement,
)


def _place_spawn_and_finalize(
    records: list[WorkerRecord],
    *,
    registry: Registry,
    backend: str,
    worker: WorkerRecord,
    expected_updated_at: str,
) -> tuple[SpawnResult, str | None, str | None]:
    """Runs `resolve_placement()`+`spawn()`'s `try`/`except CrossBufferError`
    (which must drive the worker to FAILED on failure), plus the
    `finalize_spawned_pane()` follow-up."""
    try:
        anchor_pane_ref, direction, eq_target = resolve_placement(
            records,
            backend=backend,
            parent_session_id=worker.parent_session_id,  # the record's existing value -- root_children's scope
            parent_worker_name=worker.parent_worker_name,  # the record's existing value, left unchanged
            orchestrator_ref=orchestrator_pane_ref(backend),
        )
        jsonl_model, permission_mode = resolve_last_session_state(
            worker.session_log_path
        )
        model = resolve_respawn_model(jsonl_model, worker.model)
        spawned = terminal_ops.spawn(
            backend=backend,
            command=resume_command(
                worktree=Path(worker.worktree),
                name=worker.name,
                shell=resolve_login_shell(),
                model=model,
                permission_mode=permission_mode,
            ),
            anchor_pane_ref=anchor_pane_ref,
            split_direction=direction,
            active_pane_width=(
                load_active_pane_width()
                if direction is SplitDirection.HORIZONTAL
                else None
            ),
        )
    except CrossBufferError as original:
        reraise_after_best_effort(
            original,
            "record FAILED state",
            lambda: registry.transition(
                worker.name, WorkerState.FAILED, expected_updated_at=expected_updated_at
            ),
        )
    return finalize_spawned_pane(backend=backend, spawned=spawned, eq_target=eq_target)


def _finalize_registry_record(
    registry: Registry, name: str, validated: SpawnResult, *, expected_updated_at: str
) -> WorkerRecord:
    def _mark_ready(current: WorkerRecord) -> None:
        current.attach_pane(validated.pane_ref, validated.window_ref)
        current.transition_to(WorkerState.READY)

    try:
        return registry.mutate(
            name, _mark_ready, expected_updated_at=expected_updated_at
        )
    except ConflictError as original:
        reraise_after_best_effort(
            original,
            "attach the pane to the conflicting record",
            lambda: registry.mutate(
                name,
                lambda current: current.attach_pane(
                    validated.pane_ref, validated.window_ref
                ),
            ),
        )


def respawn(
    registry: Registry, name: str, *, parent_session_id: str | None
) -> dict[str, Any]:
    records = registry.list()
    worker = next((w for w in records if w.name == name), None)
    if worker is None:
        raise NotFoundError(f"worker {name!r} is not registered")
    if not worker.may_respawn:
        # Checks `managed` first -- even if an unmanaged worker still has a
        # pane_ref, reconcile only processes managed workers, so falling
        # into that branch would be a dead end for guidance.
        if worker.managed and worker.pane_ref is not None:
            raise ValidationError(
                f"cannot respawn {worker.name!r}: a pane is still attached; "
                "run `sukuna reconcile` first"
            )
        raise ValidationError(
            f"cannot respawn {worker.name!r}: only managed workers in "
            "closed/failed/timed_out state with no attached pane can be respawned"
        )

    if resolve_existing_directory(worker.worktree) is None:
        raise ValidationError(
            f"cannot respawn {worker.name!r}: worktree {worker.worktree!r} no longer exists"
        )

    backend = detect_backend()
    reject_cross_backend_continuation(
        records, parent_session_id=parent_session_id, backend=backend
    )

    def _restart(current: WorkerRecord) -> None:
        current.transition_to(WorkerState.STARTING)
        current.session_log_path = None
        # `transition_to()` just bumped `updated_at` to this STARTING
        # transition's own timestamp -- reusing it here (rather than a
        # fresh `utc_now()` call) means `resolve_session_log_path()`'s
        # `not_before` filter and the transition it was cleared under
        # agree exactly.
        current.session_log_reset_at = current.updated_at

    expected = registry.mutate(name, _restart, expected_updated_at=worker.updated_at)

    validated, resize_warning, equalize_warning = _place_spawn_and_finalize(
        records,
        registry=registry,
        backend=backend,
        worker=worker,
        expected_updated_at=expected.updated_at,
    )

    updated = _finalize_registry_record(
        registry, name, validated, expected_updated_at=expected.updated_at
    )

    return attach_spawn_warnings(
        record_to_dict(updated),
        equalize_warning=equalize_warning,
        resize_warning=resize_warning,
    )

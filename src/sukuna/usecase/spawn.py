"""Spawn managed worker panes and register them, from a pre-validated batch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain.entity.pane import SplitDirection
from ..domain.entity.worker_record import WorkerRecord, WorkerState
from ..domain.mapper.command_mapper import worker_command
from ..domain.mapper.registry_mapper import record_to_dict
from ..domain.mapper.terminal_mapper import SpawnResult
from ..domain.service.model_validation import validate_model_membership
from ..domain.service.spawn_name_uniqueness import (
    DuplicateNameViolation,
    NameCandidate,
    find_duplicate_candidate_names,
    simulate_group_candidate_names,
)
from ..domain.service.spawn_preflight import SpawnSpec, preflight_one_spec
from ..errors import (
    ConflictError,
    CrossBufferError,
    ModelCatalogError,
    ValidationError,
)
from ..infrastructure.filesystem import resolve_existing_directory
from ..infrastructure.model_catalog import (
    fetch_model_ids,
    model_catalog_path,
    read_cached_model_ids,
    write_cached_model_ids,
)
from ..infrastructure.registry import Registry
from ..infrastructure.settings import load_active_pane_width, load_retention_days
from ..infrastructure.terminal import operations as terminal_ops
from ..infrastructure.terminal.operations import SpawnOutcome
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


def preflight_spawn_specs(
    raw_specs: list[Any], *, parent_session_id: str | None
) -> list[SpawnSpec]:
    """Statically validates every element of a spawn batch before any pane
    or registry operation runs; a single invalid element rejects the whole
    batch. `raw_specs` is the parsed JSON array from stdin. Per-element
    validation lives in `domain.service.spawn_preflight.preflight_one_spec()`;
    this loop numbers the failing element and aggregates problems."""
    if not raw_specs:
        raise ValidationError("spawn batch must contain at least one spec")
    problems: list[str] = []
    resolved: list[SpawnSpec] = []
    # Read once for the whole batch -- the setting is global, not
    # per-element, and a malformed settings file is a batch-wide
    # precondition failure rather than any one element's problem.
    default_active_pane_width = load_active_pane_width()
    for index, raw_spec in enumerate(raw_specs):
        try:
            resolved.append(
                preflight_one_spec(
                    raw_spec,
                    parent_session_id=parent_session_id,
                    default_active_pane_width=default_active_pane_width,
                    resolve_existing_directory=resolve_existing_directory,
                )
            )
        except ValidationError as error:
            problems.append(f"element {index}: {error}")
    if problems:
        raise ValidationError("; ".join(problems))
    return resolved


def _place_and_spawn_pane(
    records: list[WorkerRecord],
    *,
    backend: str,
    worktree: Path,
    worker: WorkerRecord,
    active_pane_width: int,
) -> tuple[SpawnOutcome, list[str] | None]:
    """Resolve where the new pane goes, then create it there. Both this and
    `orchestrator_pane_ref()`/`worker_command()`'s own `ValidationError`s
    must stay inside the caller's `try`/`except CrossBufferError` -- a
    failure at either step still has to drive the worker to FAILED."""
    anchor, direction, eq_target = resolve_placement(
        records,
        backend=backend,
        parent_session_id=worker.parent_session_id,
        parent_worker_name=worker.parent_worker_name,
        orchestrator_ref=orchestrator_pane_ref(backend),
    )
    # active_pane_width applies only to the "0 children -> split the parent
    # horizontally" case; a vertical (stacked) placement silently ignores
    # it -- no warning, no failure.
    percent_for_placement = (
        active_pane_width if direction is SplitDirection.HORIZONTAL else None
    )
    # `percent_for_placement` is never applied inline at spawn time --
    # `terminal_ops.spawn()` always defers it via
    # `SpawnOutcome.pending_active_pane_width`, for both backends.
    spawned = terminal_ops.spawn(
        backend=backend,
        command=worker_command(
            worktree=worktree,
            name=worker.name,
            shell=resolve_login_shell(),
            model=worker.model,
        ),
        anchor_pane_ref=anchor,
        split_direction=direction,
        active_pane_width=percent_for_placement,
    )
    return spawned, eq_target


def _finalize_registry_record(
    registry: Registry, worker: WorkerRecord, *, expected_updated_at: str
) -> None:
    try:
        registry.replace(worker, expected_updated_at=expected_updated_at)
    except ConflictError as original:
        reraise_after_best_effort(
            original,
            "attach the pane to the conflicting record",
            lambda: registry.mutate(
                worker.name,
                lambda current: current.attach_pane(worker.pane_ref, worker.window_ref),
            ),
        )


def _spawn_and_finalize_pane(
    records: list[WorkerRecord],
    *,
    registry: Registry,
    backend: str,
    spec: SpawnSpec,
    worker: WorkerRecord,
) -> tuple[SpawnResult, str | None, str | None]:
    """Runs the placement/spawn `try`/`except CrossBufferError`, which
    must drive `worker` to FAILED on failure."""
    try:
        spawned, eq_target = _place_and_spawn_pane(
            records,
            backend=backend,
            worktree=spec.worktree,
            worker=worker,
            active_pane_width=spec.active_pane_width,
        )
    except CrossBufferError as original:
        reraise_after_best_effort(
            original,
            "record FAILED state",
            lambda: registry.transition(worker.name, WorkerState.FAILED),
        )
    return finalize_spawned_pane(backend=backend, spawned=spawned, eq_target=eq_target)


def _spawn_one(
    registry: Registry,
    *,
    spec: SpawnSpec,
    name: str,
    ordinal: int,
    parent_session_id: str | None,
) -> dict[str, Any]:
    """Spawns one worker under the already-computed `name`/`ordinal` (from
    `batch.candidates_by_index`, not recomputed). `ordinal` advances the
    persisted high-water-mark atomically via `registry.add()`'s
    `claim_ordinal`. Only performs registry/backend-dependent runtime
    checks; `spec.parent_worker` is assumed already validated."""
    backend = detect_backend()
    # Read once for this whole call -- the backend-mismatch check below and
    # `_place_and_spawn_pane()` (via `_resolve_placement()`) both need the
    # "who currently holds a live pane" state, and re-reading between them
    # (a second `registry.list()`, a second file lock) would just be two
    # snapshots of the same instant. The new worker being spawned isn't in
    # `records` yet either way -- read before or after `registry.add()`
    # below, it has no `pane_ref` yet, so `alive_pane_holders()` excludes
    # it regardless.
    records = registry.list()
    reject_cross_backend_continuation(
        records, parent_session_id=parent_session_id, backend=backend
    )
    worker = WorkerRecord.create(
        name=name,
        repo_root=str(spec.repo),
        worktree=str(spec.worktree),
        parent_session_id=parent_session_id,
    )
    worker.goal = spec.goal
    worker.parent_worker_name = spec.parent_worker
    worker.model = spec.model
    registry.add(worker, claim_ordinal=ordinal)
    expected_updated_at = worker.updated_at

    validated, resize_warning, equalize_warning = _spawn_and_finalize_pane(
        records, registry=registry, backend=backend, spec=spec, worker=worker
    )

    worker.attach_pane(validated.pane_ref, validated.window_ref)
    worker.transition_to(WorkerState.READY)
    _finalize_registry_record(registry, worker, expected_updated_at=expected_updated_at)

    return attach_spawn_warnings(
        record_to_dict(worker),
        equalize_warning=equalize_warning,
        resize_warning=resize_warning,
    )


def _write_model_cache_best_effort(fresh_ids: list[str], path: Path) -> None:
    """The cache is a disposable derived artifact, not a source of truth
    (model_catalog.py module docstring; its read side already treats every
    failure as an empty cache). Validation proceeds on the in-hand
    `fresh_ids` either way — a failed refresh (disk full, permissions,
    missing state dir) must not reject the batch."""
    try:
        write_cached_model_ids(fresh_ids, path)
    except OSError:
        pass


def _validate_models_against_catalog(
    models: set[str], catalog_ids: list[str]
) -> dict[str, CrossBufferError]:
    errors: dict[str, CrossBufferError] = {}
    for model in models:
        try:
            validate_model_membership(model, catalog_ids)
        except ValidationError as error:
            errors[model] = error
    return errors


def _resolve_model_validation_errors(
    specs: list[SpawnSpec], *, api_key: str | None
) -> dict[str, CrossBufferError]:
    """Opt-in `model` membership check against the Anthropic models-list
    catalog: a no-op unless `api_key` is truthy. Runs once for the whole
    batch -- at most one `fetch_model_ids()` call and one cache read/write
    -- before any per-element work. Returns a `{model: error}` map; an
    absent `model` is unset or already known-good."""
    if not api_key:
        return {}
    unique_models = {spec.model for spec in specs if spec.model is not None}
    if not unique_models:
        return {}

    path = model_catalog_path()
    cached_ids = read_cached_model_ids(path)
    misses = {model for model in unique_models if model not in cached_ids}
    if not misses:
        return {}

    try:
        fresh_ids = fetch_model_ids(api_key=api_key)
    except ModelCatalogError as error:
        return {model: error for model in misses}

    _write_model_cache_best_effort(fresh_ids, path)
    return _validate_models_against_catalog(misses, fresh_ids)


def _registered_name_or_none(
    registry: Registry, candidate_name: str | None
) -> str | None:
    """`candidate_name` if `registry.add()` reached it before the element
    failed, else `None` -- covering `candidate_name is None` (nothing to
    look up), a pre-`registry.add()` failure (nothing to report), and the
    lookup itself hitting a registry failure (unknown whether `registry.add()`
    committed). Any of these degrades to `None` rather than raising."""
    if candidate_name is None:
        return None
    try:
        registry.get(candidate_name)
    except CrossBufferError:
        return None
    return candidate_name


def _spec_to_json(spec: SpawnSpec) -> dict[str, Any]:
    return {
        "role": spec.role,
        "repo": str(spec.repo),
        "worktree": str(spec.worktree),
        "goal": spec.goal,
        "parent_worker": spec.parent_worker,
        "active_pane_width": spec.active_pane_width,
        "model": spec.model,
    }


def _format_duplicate_name_violations(
    violations: list[DuplicateNameViolation],
) -> str:
    parts: list[str] = []
    for violation in violations:
        origin = (
            "already exists on disk"
            if violation.already_persisted
            else "computed by more than one batch element"
        )
        indices = ", ".join(str(i) for i in violation.batch_indices)
        parts.append(f"{violation.name!r} ({origin}; batch element(s) {indices})")
    return "duplicate worker name(s) detected before spawn: " + "; ".join(parts)


def _validate_batch_name_uniqueness(
    specs: list[SpawnSpec],
    resolved_registries: list[Registry],
    *,
    parent_session_id: str | None,
    model_validation_errors: dict[str, CrossBufferError],
    list_persisted_names: Callable[[], frozenset[str]] | None,
) -> dict[int, NameCandidate]:
    """Layer 1 of the cross-shard name-uniqueness validation: rejects the
    whole batch, before any commit, when two or more specs would compute
    the same worker name (in the same shard or different ones) or a
    computed name already exists on disk. Runs after
    `_purge_each_distinct_registry()` and before the per-element commit
    loop.

    On success, returns every live spec's validated `NameCandidate`
    (name + ordinal) keyed by its batch index; `_process_one_spec()`/
    `_spawn_one()` must consume it as-is, never recomputing
    `registry.next_ordinal()`/`worker_name()` at commit time. A spec
    already doomed by a model-validation error is excluded (no entry).

    Registries are resolved once by the caller (`spawn_many()`'s
    `resolved_registries`) and reused here, grouped by `Registry.path`.
    `simulate_group_candidate_names()` folds each group's specs in
    relative order from one `Registry.read_state()` snapshot per shard.

    `persisted_names` comes from `list_persisted_names()` when supplied
    (every shard); otherwise it falls back to the union of names already
    on disk in the registries this batch resolved to."""
    live = [
        (index, spec)
        for index, spec in enumerate(specs)
        if spec.model not in model_validation_errors
    ]
    groups: dict[Path, list[tuple[int, SpawnSpec]]] = {}
    for index, spec in live:
        groups.setdefault(resolved_registries[index].path, []).append((index, spec))

    candidates: list[NameCandidate] = []
    touched_names: set[str] = set()
    for group in groups.values():
        group_registry = resolved_registries[group[0][0]]
        state = group_registry.read_state()
        touched_names.update(state.workers)
        candidates.extend(
            simulate_group_candidate_names(
                group,
                records=list(state.workers.values()),
                ordinal_high_water_marks=state.ordinal_high_water_marks,
                parent_session_id=parent_session_id,
            )
        )

    persisted_names = (
        list_persisted_names()
        if list_persisted_names is not None
        else frozenset(touched_names)
    )
    violations = find_duplicate_candidate_names(candidates, persisted_names)
    if violations:
        raise ValidationError(_format_duplicate_name_violations(violations))
    return {candidate.index: candidate for candidate in candidates}


@dataclass(frozen=True)
class _SpawnBatchState:
    """Bundles `spawn_many()`'s per-batch (not per-element) state.
    `candidates_by_index` is `_validate_batch_name_uniqueness()`'s
    validated output: the name/ordinal every *live* spec must commit
    under, keyed by its batch index. A doomed spec (a key in
    `model_validation_errors`) has no entry."""

    parent_session_id: str | None
    model_validation_errors: dict[str, CrossBufferError]
    candidates_by_index: dict[int, NameCandidate]
    succeeded: list[dict[str, Any]]
    failed: list[dict[str, Any]]


def _process_one_spec(
    spec: SpawnSpec, *, index: int, this_registry: Registry, batch: _SpawnBatchState
) -> None:
    """One `spawn_many()` loop iteration's body: appends directly to
    `batch.succeeded`/`batch.failed` rather than returning a result. The
    name/ordinal come from `batch.candidates_by_index[index]` (see
    `_validate_batch_name_uniqueness()`), never recomputed here."""
    parent_session_id = batch.parent_session_id
    candidate_name: str | None = None
    try:
        if spec.model in batch.model_validation_errors:
            raise batch.model_validation_errors[spec.model]
        candidate = batch.candidates_by_index[index]
        candidate_name = candidate.name
        worker = _spawn_one(
            this_registry,
            spec=spec,
            name=candidate.name,
            ordinal=candidate.ordinal,
            parent_session_id=parent_session_id,
        )
    except CrossBufferError as error:
        entry = {
            "index": index,
            "spec": _spec_to_json(spec),
            "error": {"code": error.code, "message": str(error)},
        }
        registered_name = _registered_name_or_none(this_registry, candidate_name)
        if registered_name is not None:
            entry["worker_name"] = registered_name
        batch.failed.append(entry)
        return
    batch.succeeded.append({"index": index, "worker": worker})


def spawn_many(
    registry: Registry | Callable[[SpawnSpec], Registry],
    specs: list[SpawnSpec],
    *,
    parent_session_id: str | None,
    api_key: str | None = None,
    list_persisted_names: Callable[[], frozenset[str]] | None = None,
) -> dict[str, Any]:
    """Spawn each pre-flighted spec in `specs` in order, within one process.
    `registry` is a single `Registry` or a per-spec `Registry` resolver.
    One spec's failure (backend/registry error) fails only that element;
    the rest of the batch still runs. Runs retention purge and batch
    name-uniqueness validation once, before any element commits."""
    resolve_registry: Callable[[SpawnSpec], Registry] = (
        (lambda _spec: registry) if isinstance(registry, Registry) else registry
    )
    resolved_registries = [resolve_registry(spec) for spec in specs]
    _purge_each_distinct_registry(resolved_registries)
    model_validation_errors = _resolve_model_validation_errors(specs, api_key=api_key)
    candidates_by_index = _validate_batch_name_uniqueness(
        specs,
        resolved_registries,
        parent_session_id=parent_session_id,
        model_validation_errors=model_validation_errors,
        list_persisted_names=list_persisted_names,
    )
    batch = _SpawnBatchState(
        parent_session_id=parent_session_id,
        model_validation_errors=model_validation_errors,
        candidates_by_index=candidates_by_index,
        succeeded=[],
        failed=[],
    )
    for index, (spec, this_registry) in enumerate(
        zip(specs, resolved_registries, strict=True)
    ):
        # `preflight_spawn_specs()` is the one and only place the
        # `None`/absent -> `DEFAULT_ACTIVE_PANE_WIDTH` default is resolved;
        # every spec reaching this loop already carries a concrete int in
        # `active_pane_width`. `_process_one_spec()` -- this iteration's
        # body -- looks up this element's already-validated name/ordinal
        # (`batch.candidates_by_index`, Layer 1) and calls
        # `_spawn_one()` inside its own `try`/`except CrossBufferError` so a
        # registry failure on one element (e.g. `RegistryIOError` from a
        # disk read) fails only that element instead of escaping this loop
        # and losing prior elements' `succeeded` entries.
        _process_one_spec(spec, index=index, this_registry=this_registry, batch=batch)
    return {"succeeded": batch.succeeded, "failed": batch.failed}


def _purge_each_distinct_registry(registries: list[Registry]) -> None:
    """Retention thinning runs once per *distinct* registry path a batch
    touches, all before any spec's own `try`/`except CrossBufferError` --
    every spec's registry is resolved up front and each distinct path
    purged exactly once."""
    purged_paths: set[Path] = set()
    for reg in registries:
        if reg.path not in purged_paths:
            reg.purge(retention_days=load_retention_days(), now=datetime.now(UTC))
            purged_paths.add(reg.path)

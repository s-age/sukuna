"""Resolves which shard's `Registry` a CLI operation should use -- by
name, by spawn spec, or fanned out across every shard -- after triggering
migration once. Shard-key decisions live in
`domain/service/shard_resolution.py`; shard I/O lives in
`infrastructure/registry_shard.py`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..domain.mapper.registry_mapper import record_to_dict
from ..domain.service.shard_resolution import find_cross_shard_duplicate_names
from ..domain.service.spawn_preflight import SpawnSpec
from ..infrastructure import registry_shard
from ..infrastructure.registry import Registry
from .reconcile import reconcile as reconcile_one_shard


def ensure_migrated() -> None:
    registry_shard.migrate_if_needed()


def registry_for_worker(name: str) -> Registry:
    """Resolve `name`'s shard, falling back to the (possibly not yet
    existing) catch-all shard's path when no shard has `name` yet."""
    found = registry_shard.registry_for_name(name)
    if found is not None:
        return found
    return Registry(
        registry_shard.shard_path_for_key(registry_shard.CATCH_ALL_SHARD_KEY)
    )


def registry_for_root(*, parent_session_id: str | None, cwd: str) -> Registry:
    return registry_shard.registry_for_root_spawn(
        parent_session_id=parent_session_id, cwd=cwd
    )


def spawn_registry_resolver(
    *, parent_session_id: str | None, cwd: str
) -> Callable[[SpawnSpec], Registry]:
    """A `spawn_many()`-compatible per-spec resolver: `parent_worker`
    inherits that worker's shard (any depth) unless it would graft onto a
    different shard than this session's own (`registry_for_child_spawn()`
    raises `ValidationError` first); no `parent_worker` resolves as a ROOT
    spawn, session-reuse-first then `cwd`."""

    def resolve(spec: SpawnSpec) -> Registry:
        if spec.parent_worker:
            return registry_shard.registry_for_child_spawn(
                parent_worker_name=spec.parent_worker,
                parent_session_id=parent_session_id,
                cwd=cwd,
            )
        return registry_for_root(parent_session_id=parent_session_id, cwd=cwd)

    return resolve


def persisted_worker_names() -> frozenset[str]:
    """Every worker name already committed to any shard -- the "already on
    disk somewhere" half of spawn's Layer 1 cross-shard name-uniqueness
    check (`domain/service/spawn_name_uniqueness.py`). `cli.py`'s
    `handle_spawn` passes this in only for the real sharded path (never
    with `--registry`, where a single file is the whole universe already)."""
    names: set[str] = set()
    for summary in registry_shard.read_all_shard_summaries():
        names.update(summary.names)
    return frozenset(names)


def all_workers_view() -> Registry:
    """A read-only `Registry` merging every shard's records, for `tree`/
    `inspect` with no `--worker`. Never call a mutating method on the
    result -- see `MergedRegistryView`."""
    return registry_shard.MergedRegistryView(registry_shard.all_shard_registries())


def _duplicate_names_report(shards: list[Registry]) -> list[dict[str, Any]]:
    """Layer 2 of the cross-shard name-uniqueness validation: report
    (never repair -- see `find_cross_shard_duplicate_names()`'s docstring)
    every worker `name` held by more than one shard. Reads each shard
    fresh (`shard.list()`), not any snapshot the reconcile pass above may
    have taken, so a `prune=True` sweep's deletions are reflected."""
    shard_records = [(str(shard.path), shard.list()) for shard in shards]
    groups = find_cross_shard_duplicate_names(shard_records)
    return [
        {
            "name": group.name,
            "workers": [
                {**record_to_dict(record), "shard_path": shard_path}
                for shard_path, record in group.entries
            ],
        }
        for group in groups
    ]


def reconcile_all_shards(
    *, prune: bool = False, now: datetime | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Runs `reconcile()` once per real shard `Registry` (never a
    `MergedRegistryView`, which cannot safely write) and concatenates each
    shard's three result lists. Also reports every worker name found in
    more than one shard under the `duplicate_names` key -- detection only,
    no automatic repair (see `_duplicate_names_report()`)."""
    reconciled: list[dict[str, Any]] = []
    pane_cleared: list[dict[str, Any]] = []
    purged: list[dict[str, Any]] = []
    shards = registry_shard.all_shard_registries()
    for shard in shards:
        result = reconcile_one_shard(shard, prune=prune, now=now)
        reconciled.extend(result["reconciled"])
        pane_cleared.extend(result["pane_cleared"])
        if prune:
            purged.extend(result.get("purged", []))
    merged: dict[str, list[dict[str, Any]]] = {
        "reconciled": reconciled,
        "pane_cleared": pane_cleared,
        "duplicate_names": _duplicate_names_report(shards),
    }
    if prune:
        merged["purged"] = purged
    return merged

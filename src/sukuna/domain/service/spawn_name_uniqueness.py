"""Pure batch-wide name-uniqueness checks for spawn preflight.

`usecase/spawn.py` groups a batch's specs by resolved shard registry and
hands each group's real, persisted state to
`simulate_group_candidate_names()`, which reproduces in memory -- without
touching the registry -- the same sequential ordinal/name allocation the
real commit loop (`Registry.add()`'s `claim_ordinal` handling) performs.
`find_duplicate_candidate_names()` then checks the resulting names,
across every group, for collisions within the batch and against names
already persisted on disk. Both functions reuse `spawn_placement.py`'s
`next_ordinal()`/`advance_ordinal_high_water_mark()`/`worker_name()`."""

from __future__ import annotations

from dataclasses import dataclass

from ..entity.worker_record import WorkerRecord
from .spawn_placement import (
    advance_ordinal_high_water_mark,
    next_ordinal,
    worker_name,
)
from .spawn_preflight import SpawnSpec


@dataclass(frozen=True)
class NameCandidate:
    """One spec's simulated outcome: `index` is its position in the
    caller's original batch (not the shard-group-local position). `ordinal`
    is carried alongside `name` so the caller (`usecase/spawn.py`) feeds
    both straight into the real commit
    (`Registry.add(claim_ordinal=...)`); the committed name must be
    byte-for-byte this validated candidate, never a freshly recomputed
    `next_ordinal()`/`worker_name()` call."""

    index: int
    name: str
    ordinal: int


def simulate_group_candidate_names(
    indexed_specs: list[tuple[int, SpawnSpec]],
    *,
    records: list[WorkerRecord],
    ordinal_high_water_marks: dict[str, int],
    parent_session_id: str | None,
) -> list[NameCandidate]:
    """Reproduce, in memory, the sequential ordinal/name allocation the
    real per-element commit loop performs for every spec in
    `indexed_specs` that resolves to *one* shard registry. `records`/
    `ordinal_high_water_marks` must be that shard's real, currently
    persisted state; this function mutates only local copies. `indexed_
    specs` must already be in the batch's original relative order for
    this shard -- this function does not sort or reorder them. For each
    spec, in order: compute its ordinal via `next_ordinal()`, derive its
    name via `worker_name()`, then apply both state changes a real
    `Registry.add(worker, claim_ordinal=ordinal)` would make (append a
    record, advance the high-water-mark floor) before simulating the next
    spec. Both state changes must be applied, or a shard whose floor
    already exceeds its record count can produce a false-positive
    rejection of a legitimate multi-spec batch.
    """
    sim_records = list(records)
    sim_hwm = dict(ordinal_high_water_marks)
    candidates: list[NameCandidate] = []
    for index, spec in indexed_specs:
        ordinal = next_ordinal(sim_records, sim_hwm, parent_session_id)
        name = worker_name(spec.repo, parent_session_id, spec.role, ordinal)
        candidates.append(NameCandidate(index=index, name=name, ordinal=ordinal))
        sim_hwm = advance_ordinal_high_water_mark(
            sim_hwm, parent_session_id=parent_session_id, claimed_ordinal=ordinal
        )
        sim_records.append(
            WorkerRecord.create(
                name=name,
                repo_root=str(spec.repo),
                worktree=str(spec.worktree),
                parent_session_id=parent_session_id,
            )
        )
    return candidates


@dataclass(frozen=True)
class DuplicateNameViolation:
    """One candidate name that collides -- either two or more batch
    elements (possibly in different shard groups) independently computed
    it, or it was computed once but already exists on disk in some shard.
    `batch_indices` is sorted and always non-empty; a purely-persisted
    collision with no second batch element still has exactly one index
    here."""

    name: str
    batch_indices: tuple[int, ...]
    already_persisted: bool


def find_duplicate_candidate_names(
    candidates: list[NameCandidate], persisted_names: frozenset[str]
) -> list[DuplicateNameViolation]:
    """A name is a violation when its batch occurrence count is more than
    one, or when it occurs even once but is already in `persisted_names`
    (every name already committed to disk, anywhere). Checks the full
    candidate multiset without regard to which shard group a candidate
    came from -- a superset check, so it stays correct even if
    `simulate_group_candidate_names()`'s same-group simulation is ever
    imperfect."""
    indices_by_name: dict[str, list[int]] = {}
    for candidate in candidates:
        indices_by_name.setdefault(candidate.name, []).append(candidate.index)
    violations: list[DuplicateNameViolation] = []
    for name, indices in sorted(indices_by_name.items()):
        persisted = name in persisted_names
        if len(indices) > 1 or persisted:
            violations.append(
                DuplicateNameViolation(
                    name=name,
                    batch_indices=tuple(sorted(indices)),
                    already_persisted=persisted,
                )
            )
    return violations

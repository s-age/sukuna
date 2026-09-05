"""Pure unit tests for Layer 1's building blocks:
`simulate_group_candidate_names()` (in-memory ordinal/name allocation,
mirroring what a real `Registry.add(claim_ordinal=...)` commit loop would
do) and `find_duplicate_candidate_names()` (multiset + persisted-name
collision detection). `usecase/spawn.py`'s own tests cover the wiring --
grouping specs by resolved shard, batch-wide rejection, doomed-spec
exclusion."""

from __future__ import annotations

from pathlib import Path

from sukuna.domain.entity.worker_record import WorkerRecord
from sukuna.domain.service.spawn_name_uniqueness import (
    NameCandidate,
    find_duplicate_candidate_names,
    simulate_group_candidate_names,
)
from sukuna.domain.service.spawn_placement import worker_name
from sukuna.domain.service.spawn_preflight import SpawnSpec

_REPO = Path("/repo")


def _spec(*, role: str = "review") -> SpawnSpec:
    return SpawnSpec(
        role=role,
        repo=_REPO,
        worktree=_REPO,
        goal=None,
        parent_worker=None,
        active_pane_width=50,
        model=None,
    )


def test_simulate_group_candidate_names_assigns_sequential_ordinals_from_empty_state() -> (
    None
):
    candidates = simulate_group_candidate_names(
        [(0, _spec()), (1, _spec())],
        records=[],
        ordinal_high_water_marks={},
        parent_session_id="parent-1",
    )

    assert [c.name for c in candidates] == [
        worker_name(_REPO, "parent-1", "review", 1),
        worker_name(_REPO, "parent-1", "review", 2),
    ]
    assert [c.index for c in candidates] == [0, 1]


def test_simulate_group_candidate_names_advances_the_floor_between_specs() -> None:
    """Reproduces a shard with zero records but a floor already ahead of
    the record count (what a retention purge leaves behind). Simulating
    only the records-view (and not
    also advancing the floor) would make both specs collide on ordinal 10 --
    they must instead land on 10 and 11, matching what the real commit loop
    computes via `Registry.add(claim_ordinal=...)`."""
    candidates = simulate_group_candidate_names(
        [(0, _spec()), (1, _spec())],
        records=[],
        ordinal_high_water_marks={"parent-1": 10},
        parent_session_id="parent-1",
    )

    assert [c.name for c in candidates] == [
        worker_name(_REPO, "parent-1", "review", 10),
        worker_name(_REPO, "parent-1", "review", 11),
    ]


def test_simulate_group_candidate_names_does_not_mutate_its_inputs() -> None:
    records = [
        WorkerRecord.create(
            name="ccw-existing",
            repo_root=str(_REPO),
            worktree=str(_REPO),
            parent_session_id="parent-1",
        )
    ]
    hwm = {"parent-1": 5}

    simulate_group_candidate_names(
        [(0, _spec())],
        records=records,
        ordinal_high_water_marks=hwm,
        parent_session_id="parent-1",
    )

    assert records == [records[0]]
    assert len(records) == 1
    assert hwm == {"parent-1": 5}


def test_find_duplicate_candidate_names_flags_an_in_batch_collision() -> None:
    candidates = [
        NameCandidate(index=0, name="ccw-x-review-1", ordinal=1),
        NameCandidate(index=2, name="ccw-x-review-1", ordinal=1),
        NameCandidate(index=1, name="ccw-y-review-1", ordinal=1),
    ]

    violations = find_duplicate_candidate_names(candidates, frozenset())

    assert len(violations) == 1
    (violation,) = violations
    assert violation.name == "ccw-x-review-1"
    assert violation.batch_indices == (0, 2)
    assert violation.already_persisted is False


def test_find_duplicate_candidate_names_flags_a_single_element_colliding_with_disk() -> (
    None
):
    candidates = [NameCandidate(index=0, name="ccw-x-review-1", ordinal=1)]

    violations = find_duplicate_candidate_names(
        candidates, frozenset({"ccw-x-review-1"})
    )

    assert len(violations) == 1
    (violation,) = violations
    assert violation.batch_indices == (0,)
    assert violation.already_persisted is True


def test_find_duplicate_candidate_names_is_empty_for_a_clean_batch() -> None:
    candidates = [
        NameCandidate(index=0, name="ccw-x-review-1", ordinal=1),
        NameCandidate(index=1, name="ccw-x-review-2", ordinal=2),
    ]

    violations = find_duplicate_candidate_names(candidates, frozenset())

    assert violations == []

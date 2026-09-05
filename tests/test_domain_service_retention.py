from datetime import UTC, datetime, timedelta

import pytest

from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.domain.service.retention import (
    DEFAULT_RETENTION_DAYS,
    is_retirement_candidate,
    seed_ordinal_floors_before_purge,
    sweep_is_due,
    sweep_stale_records,
    validate_retention_days,
)
from sukuna.errors import ValidationError

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def worker(
    *,
    name: str = "ccw-project-00000000-review-1",
    state: WorkerState = WorkerState.CLOSED,
    updated_at: datetime = NOW,
    pane_ref: str | None = None,
    parent_session_id: str | None = "session-a",
) -> WorkerRecord:
    return WorkerRecord(
        name=name,
        repo_root="/repo",
        worktree="/repo",
        updated_at=updated_at.isoformat(),
        state=state,
        pane_ref=pane_ref,
        parent_session_id=parent_session_id,
    )


def test_validate_retention_days_accepts_a_positive_integer() -> None:
    assert validate_retention_days(30) == 30


def test_validate_retention_days_rejects_zero() -> None:
    """0 is rejected -- `reconcile --prune` already covers "clean up right
    now"."""
    with pytest.raises(ValidationError):
        validate_retention_days(0)


def test_validate_retention_days_rejects_a_negative_value() -> None:
    with pytest.raises(ValidationError):
        validate_retention_days(-1)


def test_validate_retention_days_rejects_a_non_integer() -> None:
    with pytest.raises(ValidationError):
        validate_retention_days("30")


def test_validate_retention_days_rejects_a_bool() -> None:
    with pytest.raises(ValidationError):
        validate_retention_days(True)


def test_is_retirement_candidate_purges_an_old_closed_worker() -> None:
    old = worker(state=WorkerState.CLOSED, updated_at=NOW - timedelta(days=31))

    assert is_retirement_candidate(old, now=NOW, retention_days=30) is True


def test_is_retirement_candidate_purges_an_old_failed_worker_without_a_pane() -> None:
    old = worker(
        state=WorkerState.FAILED, updated_at=NOW - timedelta(days=31), pane_ref=None
    )

    assert is_retirement_candidate(old, now=NOW, retention_days=30) is True


def test_is_retirement_candidate_keeps_an_old_failed_worker_with_a_live_pane() -> None:
    """Owner ruling (card Q3): FAILED additionally requires `pane_ref is
    None` -- `reconcile_filter.py`'s `stale_pane_candidates()` already
    documents that a FAILED worker's pane can still be alive or under human
    inspection."""
    old = worker(
        state=WorkerState.FAILED, updated_at=NOW - timedelta(days=31), pane_ref="%5"
    )

    assert is_retirement_candidate(old, now=NOW, retention_days=30) is False


def test_is_retirement_candidate_keeps_an_old_timed_out_worker() -> None:
    """Owner ruling: TIMED_OUT is deliberately excluded, not folded in via
    `TERMINAL_STATES`."""
    old = worker(state=WorkerState.TIMED_OUT, updated_at=NOW - timedelta(days=31))

    assert is_retirement_candidate(old, now=NOW, retention_days=30) is False


def test_is_retirement_candidate_keeps_a_recent_closed_worker() -> None:
    recent = worker(state=WorkerState.CLOSED, updated_at=NOW - timedelta(days=1))

    assert is_retirement_candidate(recent, now=NOW, retention_days=30) is False


def test_is_retirement_candidate_keeps_a_live_managed_worker_regardless_of_age() -> (
    None
):
    old_ready = worker(state=WorkerState.READY, updated_at=NOW - timedelta(days=365))

    assert is_retirement_candidate(old_ready, now=NOW, retention_days=30) is False


def test_is_retirement_candidate_boundary_is_inclusive() -> None:
    """Exactly `retention_days` old (to the second) is already purgeable."""
    exactly_at_boundary = worker(
        state=WorkerState.CLOSED, updated_at=NOW - timedelta(days=30)
    )

    assert (
        is_retirement_candidate(exactly_at_boundary, now=NOW, retention_days=30) is True
    )


def test_sweep_stale_records_partitions_survivors_and_purged() -> None:
    old_closed = worker(
        name="ccw-project-00000000-review-1", updated_at=NOW - timedelta(days=40)
    )
    recent_closed = worker(
        name="ccw-project-00000000-review-2", updated_at=NOW - timedelta(days=1)
    )

    path, survivors, purged = sweep_stale_records(
        "registry.json", [old_closed, recent_closed], retention_days=30, now=NOW
    )

    assert path == "registry.json"
    assert survivors == [recent_closed]
    assert purged == [old_closed]


def test_seed_ordinal_floors_before_purge_raises_the_floor_to_count_plus_one() -> None:
    """The core 140D89AB-reopened-by-retention scenario: a session with 3
    pre-v3 records (no high-water-mark entries at all) has all 3 purged --
    the floor must land at 4 (3 + 1), not 0."""
    records = [
        worker(name=f"ccw-project-00000000-review-{i}", parent_session_id="session-a")
        for i in (1, 2, 3)
    ]

    updated = seed_ordinal_floors_before_purge({}, records, purged=records)

    assert updated == {"session-a": 4}


def test_seed_ordinal_floors_before_purge_only_touches_affected_sessions() -> None:
    session_a = worker(name="ccw-a", parent_session_id="session-a")
    session_b = worker(name="ccw-b", parent_session_id="session-b")

    updated = seed_ordinal_floors_before_purge(
        {"session-b": 9}, [session_a, session_b], purged=[session_a]
    )

    assert updated == {"session-a": 2, "session-b": 9}


def test_seed_ordinal_floors_before_purge_never_lowers_an_existing_floor() -> None:
    records = [worker(name="ccw-a", parent_session_id="session-a")]

    updated = seed_ordinal_floors_before_purge(
        {"session-a": 99}, records, purged=records
    )

    assert updated == {"session-a": 99}


def test_seed_ordinal_floors_before_purge_does_not_mutate_the_input() -> None:
    original = {"session-a": 1}
    records = [worker(name="ccw-a", parent_session_id="session-a")]

    seed_ordinal_floors_before_purge(original, records, purged=records)

    assert original == {"session-a": 1}


def test_seed_ordinal_floors_before_purge_handles_the_null_session_key() -> None:
    records = [worker(name="ccw-a", parent_session_id=None)]

    updated = seed_ordinal_floors_before_purge({}, records, purged=records)

    assert updated == {"(none)": 2}


def test_sweep_is_due_when_never_swept() -> None:
    assert sweep_is_due(None, now=NOW) is True


def test_sweep_is_due_is_false_within_the_same_utc_day() -> None:
    assert (
        sweep_is_due(NOW.replace(hour=1).isoformat(), now=NOW.replace(hour=23)) is False
    )


def test_sweep_is_due_is_true_on_a_new_utc_day() -> None:
    yesterday = (NOW - timedelta(days=1)).isoformat()

    assert sweep_is_due(yesterday, now=NOW) is True


def test_default_retention_days_is_thirty() -> None:
    assert DEFAULT_RETENTION_DAYS == 30

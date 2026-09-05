"""CLOSED/FAILED worker record retention: which records are stale, and the
ordinal high-water-mark floors that must be raised before dropping them.
Pure functions only -- no filesystem access, no `datetime.now()` calls.
`Registry.purge()` is the sole caller that deletes anything; this module
only decides *what* would be purged."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypeVar

from ...errors import ValidationError
from ..entity.worker_record import WorkerRecord, WorkerState
from .spawn_placement import ordinal_high_water_mark_key

# Only CLOSED/FAILED are retention candidates -- TIMED_OUT is deliberately
# excluded, not folded into this via `TERMINAL_STATES`.
_RETIRABLE_STATES = frozenset({WorkerState.CLOSED, WorkerState.FAILED})

DEFAULT_RETENTION_DAYS = 30

PathT = TypeVar("PathT")


def validate_retention_days(value: object) -> int:
    """Same shape as `spawn_placement.validate_active_pane_width()`. `0` is
    rejected; `retention_days` must be a positive integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("retention_days must be an integer")
    if value < 1:
        raise ValidationError("retention_days must be a positive integer")
    return value


def is_retirement_candidate(
    worker: WorkerRecord, *, now: datetime, retention_days: int
) -> bool:
    """CLOSED or FAILED (never TIMED_OUT), aged past `retention_days` by
    `updated_at`. FAILED additionally requires `pane_ref is None` -- a
    FAILED worker's pane can still be alive or under human inspection.
    This extra condition is a no-op for CLOSED records, since `close()`
    always clears `pane_ref` on success."""
    if worker.state not in _RETIRABLE_STATES:
        return False
    if worker.state is WorkerState.FAILED and worker.pane_ref is not None:
        return False
    updated_at = datetime.fromisoformat(worker.updated_at)
    return updated_at <= now - timedelta(days=retention_days)


def sweep_stale_records(
    path: PathT,
    records: list[WorkerRecord],
    *,
    retention_days: int,
    now: datetime,
) -> tuple[PathT, list[WorkerRecord], list[WorkerRecord]]:
    """Storage-agnostic sweep: partitions `records` into survivors/purged.
    `path` passes through unchanged, identifying which file `records`
    came from."""
    survivors: list[WorkerRecord] = []
    purged: list[WorkerRecord] = []
    for record in records:
        if is_retirement_candidate(record, now=now, retention_days=retention_days):
            purged.append(record)
        else:
            survivors.append(record)
    return path, survivors, purged


def seed_ordinal_floors_before_purge(
    ordinal_high_water_marks: dict[str, int],
    records_before_purge: list[WorkerRecord],
    purged: list[WorkerRecord],
) -> dict[str, int]:
    """Before `purged` records actually disappear, raise the floor for
    every `parent_session_id` losing at least one record to at least
    `count_before_purge + 1` -- the value `next_ordinal()`'s record-count
    half would have returned pre-purge. Returns a new dict; does not
    mutate the input."""
    affected_sessions = {record.parent_session_id for record in purged}
    updated = dict(ordinal_high_water_marks)
    for parent_session_id in affected_sessions:
        key = ordinal_high_water_mark_key(parent_session_id)
        count_before_purge = sum(
            1
            for record in records_before_purge
            if record.parent_session_id == parent_session_id
        )
        updated[key] = max(updated.get(key, 0), count_before_purge + 1)
    return updated


def sweep_is_due(last_swept_at: str | None, *, now: datetime) -> bool:
    """Throttle for the spawn-path automatic sweep: at most once per UTC
    calendar day. `None` (never swept) is always due. `reconcile --prune`
    bypasses this entirely (its caller never calls this function) so a
    shortened `retention_days` can take effect immediately."""
    if last_swept_at is None:
        return True
    last = datetime.fromisoformat(last_swept_at)
    return last.astimezone(UTC).date() != now.astimezone(UTC).date()

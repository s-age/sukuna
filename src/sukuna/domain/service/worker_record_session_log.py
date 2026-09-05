"""Business rule for lazily resolving a worker's session_log_path at state
transition time -- filesystem-free, `resolve` is injected (same boundary
policy as `session_log.py`)."""

from __future__ import annotations

from collections.abc import Callable

from ..entity.worker_record import WorkerRecord, WorkerState


def attach_session_log_path_if_unresolved(
    record: WorkerRecord,
    resolve: Callable[[str, str, str | None], str | None],
) -> None:
    """No-op if `session_log_path` is already set, or `record` is still
    `STARTING`. Otherwise calls `resolve(worktree, name,
    session_log_reset_at)`; on a match, sets `session_log_path` and clears
    `session_log_reset_at`. Exceptions from `resolve` propagate uncaught."""
    if record.session_log_path is not None:
        return
    if record.state is WorkerState.STARTING:
        return
    candidate = resolve(record.worktree, record.name, record.session_log_reset_at)
    if candidate is not None:
        record.session_log_path = candidate
        record.session_log_reset_at = None

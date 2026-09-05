"""Worker records and allowed lifecycle transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from ...errors import StateError


class WorkerState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    REPORTED = "reported"
    ACCEPTED = "accepted"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CLOSED = "closed"


_ALLOWED_TRANSITIONS: dict[WorkerState, set[WorkerState]] = {
    WorkerState.STARTING: {
        WorkerState.READY,
        WorkerState.FAILED,
        WorkerState.TIMED_OUT,
    },
    WorkerState.READY: {WorkerState.BUSY, WorkerState.FAILED, WorkerState.TIMED_OUT},
    WorkerState.BUSY: {WorkerState.REPORTED, WorkerState.FAILED, WorkerState.TIMED_OUT},
    WorkerState.REPORTED: {
        WorkerState.ACCEPTED,
        WorkerState.BUSY,
        WorkerState.FAILED,
        WorkerState.TIMED_OUT,
    },
    WorkerState.ACCEPTED: {WorkerState.CLOSED, WorkerState.FAILED},
    WorkerState.FAILED: {WorkerState.STARTING},
    WorkerState.TIMED_OUT: {WorkerState.STARTING},
    WorkerState.CLOSED: {WorkerState.STARTING},
}

# Explicit set. Consumers: `reconcile_filter.py`'s `_STALE_PANE_STATES`
# and this module's `may_respawn`.
TERMINAL_STATES: frozenset[WorkerState] = frozenset(
    {WorkerState.FAILED, WorkerState.TIMED_OUT, WorkerState.CLOSED}
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class WorkerRecord:
    """`name` is the sole identity and registry primary key: the value
    `claude -n` sets, `ListAgents`/`SendMessage` address, and `claude
    --resume` searches by -- no separate worker/run id exists. There is
    no `role` field; role lives only inside `name` (`worker_name()`) for
    display. `goal` is free text, registry-only, never passed to `claude`."""

    name: str
    repo_root: str
    worktree: str
    updated_at: str
    state: WorkerState
    pane_ref: str | None = None
    window_ref: str | None = None
    managed: bool = True
    parent_session_id: str | None = None
    goal: str | None = None
    parent_worker_name: str | None = None
    model: str | None = None
    session_log_path: str | None = None
    session_log_reset_at: str | None = None

    @classmethod
    def create(
        cls,
        *,
        name: str,
        repo_root: str,
        worktree: str,
        parent_session_id: str | None = None,
    ) -> WorkerRecord:
        """Construct a STARTING record with the fields every caller sets.
        `goal`/`parent_worker_name`/`model` are optional; set them as
        dataclass attributes after construction."""
        return cls(
            name=name,
            repo_root=repo_root,
            worktree=worktree,
            updated_at=utc_now(),
            state=WorkerState.STARTING,
            parent_session_id=parent_session_id,
        )

    def transition_to(self, target: WorkerState) -> None:
        if target not in _ALLOWED_TRANSITIONS[self.state]:
            raise StateError(f"cannot transition {self.state.value} to {target.value}")
        self.state = target
        self.updated_at = utc_now()

    def attach_pane(self, pane_ref: str | None, window_ref: str | None) -> None:
        """Symmetric with `detach_pane()`: record a pane this worker now
        owns -- used both for the initial spawn (READY transition) and to
        re-attach after a lost `registry.replace()` race (`ConflictError`
        compensation)."""
        self.pane_ref = pane_ref
        self.window_ref = window_ref

    def detach_pane(self, *, clear_window: bool = False) -> None:
        """Forget this record's pane, without touching `state`. `pane_ref`
        always clears -- `alive_pane_holders()` keys its live-child/anchor
        logic on it, so a record must never point at a pane known to be
        gone. `window_ref` only clears when `clear_window` is requested:
        close's success path passes it; other callers leave it as-is."""
        self.pane_ref = None
        if clear_window:
            self.window_ref = None

    @property
    def may_close(self) -> bool:
        return (
            self.managed and self.state is WorkerState.ACCEPTED and bool(self.pane_ref)
        )

    @property
    def may_respawn(self) -> bool:
        return self.managed and self.state in TERMINAL_STATES and self.pane_ref is None

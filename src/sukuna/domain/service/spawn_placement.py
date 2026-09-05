from __future__ import annotations

from pathlib import Path

from ...errors import ValidationError
from ..entity.pane import SplitDirection
from ..entity.worker_record import WorkerRecord, WorkerState


def alive_pane_holders(records: list[WorkerRecord]) -> list[WorkerRecord]:
    """Restricts to workers that hold a pane (`pane_ref is not None`) and
    are not CLOSED. One caller (`usecase/spawn_shared.py`'s
    `reject_cross_backend_continuation`) passes a list already
    state-filtered by `existing_workers()`, making the CLOSED filter
    redundant but harmless there."""
    return [
        w
        for w in records
        if w.pane_ref is not None and w.state is not WorkerState.CLOSED
    ]


def existing_workers(
    records: list[WorkerRecord], parent_session_id: str | None
) -> list[WorkerRecord]:
    return [
        worker
        for worker in records
        if worker.parent_session_id == parent_session_id
        and worker.managed
        and worker.state is not WorkerState.CLOSED
    ]


_NESTED_CALLER_PANE_STATES = frozenset(
    {
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
    }
)


def is_nested_caller_pane_state(state: WorkerState) -> bool:
    """Liveness check specific to `usecase/state.py`'s `_dispatch_focus()`
    -- deliberately a different set than `alive_pane_holders()`. tmux
    renumbers pane ids across a server restart, so a stale `pane_ref` from
    the STARTING/FAILED/TIMED_OUT states `alive_pane_holders()` includes
    can collide with a different, newer worker's `pane_ref`. This check
    ("is the caller itself a live pane holder?") excludes those states for
    that reason."""
    return state in _NESTED_CALLER_PANE_STATES


def matches_alive_pane_ref(records: list[WorkerRecord], pane_ref: str) -> bool:
    """Pure check used by `usecase/resize.py`'s self path for
    sukuna-relatedness (gate 2) -- extracted to the domain layer from the
    equivalent inline match already present in `usecase/state.py`'s
    `_dispatch_focus()` (`any(record.pane_ref == own_pane_ref and
    is_nested_caller_pane_state(record.state) for record in workers)`)."""
    return any(
        record.pane_ref == pane_ref and is_nested_caller_pane_state(record.state)
        for record in records
    )


def worker_name(
    repo: Path, parent_session_id: str | None, role: str, ordinal: int
) -> str:
    """Non-ASCII input (a repo directory name, an unusually-shaped
    `$CLAUDE_CODE_SESSION_ID`) must never reach `_NAME_RE` unsanitized --
    `str.isalnum()` accepts Unicode letters/digits generally. Both the
    slug and the token are restricted to ASCII alnum, with a fallback for
    the case where that filter removes every character."""
    slug = (
        "".join(
            character if character.isascii() and character.isalnum() else "-"
            for character in repo.name.lower()
        ).strip("-")
        or "repo"
    )
    raw_token = (parent_session_id or "manual").split("-", maxsplit=1)[0]
    token = (
        "".join(
            character
            for character in raw_token.lower()
            if character.isascii() and character.isalnum()
        )[:8]
        or "session"
    )
    return f"ccw-{slug[:32]}-{token}-{role}-{ordinal}"


_NULL_PARENT_SESSION_ORDINAL_KEY = "(none)"


def ordinal_high_water_mark_key(parent_session_id: str | None) -> str:
    """`ordinal_high_water_marks` dict key for `parent_session_id`. JSON
    object keys must be strings, so `None` (manual/no-session spawn) needs
    a stand-in -- `"(none)"` mirrors `worker_tree.py`'s paren-sentinel
    convention (`"(unknown parent)"`, `"(orphaned: ...)"`): a real
    `$CLAUDE_CODE_SESSION_ID` never looks like this."""
    return (
        parent_session_id
        if parent_session_id is not None
        else _NULL_PARENT_SESSION_ORDINAL_KEY
    )


def advance_ordinal_high_water_mark(
    ordinal_high_water_marks: dict[str, int],
    *,
    parent_session_id: str | None,
    claimed_ordinal: int,
) -> dict[str, int]:
    """Raise `parent_session_id`'s floor to at least `claimed_ordinal + 1`
    -- never lowers it. Called by `Registry.add()`'s `claim_ordinal`
    handling for every successful spawn; the `+1` is what makes
    `next_ordinal()`'s `max(record_based, persisted_floor)` never hand
    back an already-claimed ordinal. Returns a new dict; does not mutate
    the input."""
    key = ordinal_high_water_mark_key(parent_session_id)
    updated = dict(ordinal_high_water_marks)
    updated[key] = max(updated.get(key, 0), claimed_ordinal + 1)
    return updated


def next_ordinal(
    records: list[WorkerRecord],
    ordinal_high_water_marks: dict[str, int],
    parent_session_id: str | None,
) -> int:
    """Naming ordinal for `worker_name()`: the larger of two independent
    lower bounds. Record-count-based: counts every record ever registered
    under this parent session, regardless of state or `managed` (CLOSED/
    FAILED/unmanaged included, as long as retention hasn't purged them
    yet). Persisted high-water-mark: `ordinal_high_water_marks[key]` is a
    floor that only ever grows (advanced to `claimed_ordinal + 1` on
    every successful `Registry.add()`, and seeded ahead of a retention
    purge dropping records the count above would otherwise lose track
    of) -- without it, purging a session's own old records would let a
    resumed session recompute a lower ordinal and collide with an
    already-spoken-for name. `worker_name()` deterministically reproduces
    the same name for the same (repo, session, role, ordinal) tuple, and
    `Registry.add()` rejects any name already on disk regardless of that
    record's state."""
    record_based = (
        len([w for w in records if w.parent_session_id == parent_session_id]) + 1
    )
    persisted_floor = ordinal_high_water_marks.get(
        ordinal_high_water_mark_key(parent_session_id), 0
    )
    return max(record_based, persisted_floor)


def children_of(
    pane_holders: list[WorkerRecord], parent_worker_name: str
) -> list[WorkerRecord]:
    """`pane_holders`'s (must be passed in spawn order = registry.list()
    order, preserved) members that are live children of
    `parent_worker_name`."""
    return [w for w in pane_holders if w.parent_worker_name == parent_worker_name]


def root_children(
    pane_holders: list[WorkerRecord], parent_session_id: str | None
) -> list[WorkerRecord]:
    """The "parent" of a spawn with no `parent_worker_name` is the
    orchestrator itself (which holds no worker record in the registry).
    Its live children are identified by `not parent_worker_name` (`None`
    or empty string) `and parent_session_id` matching -- the same
    truthiness condition `worker_tree.build_tree()` uses for its
    `by_parent` root determination. An empty-string `parent_worker_name`
    is rejected both by the spawn path (`validate_name()`) and by the
    document boundary (`_parent_worker_name_matches_naming_convention`,
    registry_mapper.py), so it never enters the typed world."""
    return [
        w
        for w in pane_holders
        if not w.parent_worker_name and w.parent_session_id == parent_session_id
    ]


def column_of(
    pane_holders: list[WorkerRecord],
    *,
    parent_worker_name: str | None,
    parent_session_id: str | None,
) -> list[WorkerRecord]:
    """Determines "a given record's sibling column" (`children_of` when
    `parent_worker_name` is given, `root_children` otherwise). Uses
    truthiness (`if parent_worker_name:`) -- aligned with
    `root_children()`'s own convention. Since an empty-string
    `parent_worker_name` never enters the typed world, `is None` and
    truthiness are equivalent here."""
    if parent_worker_name:
        return children_of(pane_holders, parent_worker_name)
    return root_children(pane_holders, parent_session_id)


def blocking_children_names(
    pane_holders: list[WorkerRecord], parent_worker_name: str
) -> str | None:
    """Live children of `parent_worker_name`, formatted for a close-guard
    error message; `None` when there are none. `pane_holders` must already
    be `alive_pane_holders()` output (spawn order preserved, matches
    `children_of()`'s contract)."""
    blockers = children_of(pane_holders, parent_worker_name)
    if not blockers:
        return None
    return ", ".join(sorted(child.name for child in blockers))


def orphaned_and_column(
    records: list[WorkerRecord], worker: WorkerRecord
) -> tuple[list[WorkerRecord], list[WorkerRecord]]:
    """Derives two things from one post-close registry re-read (`records`):
    children of `worker` that turned up after the live-child guard and
    before pane destruction, now orphaned; and the sibling column
    `worker`'s own parent (or session root, when `worker` has none) still
    has, for `equalize_target()` to size against."""
    pane_holders = alive_pane_holders(records)
    orphaned = children_of(pane_holders, worker.name)
    column = column_of(
        pane_holders,
        parent_worker_name=worker.parent_worker_name,
        parent_session_id=worker.parent_session_id,
    )
    return orphaned, column


def orphaned_children_message(orphaned: list[WorkerRecord]) -> str:
    """Formats the `close()`-time `orphaned_children_warning` body for a
    non-empty `orphaned` list."""
    names = ", ".join(sorted(child.name for child in orphaned))
    return (
        "child worker(s) appeared after the live-child guard and before "
        f"pane destruction, now orphaned: {names}; handle each via "
        "its normal lifecycle (check state, wait for report, accept, close)"
    )


def equalize_target(children: list[WorkerRecord]) -> list[str] | None:
    """No equalization target when `children` is empty (a brand-new column
    was just formed). Otherwise returns the existing children's
    `pane_ref`s in spawn order -- the new pane itself is only known after
    spawn succeeds, so the caller appends it at the end."""
    if not children:
        return None
    refs: list[str] = []
    for child in children:
        if child.pane_ref is None:
            raise ValidationError(
                f"equalize_target: child {child.name!r} has no pane_ref "
                "(caller must pass alive_pane_holders() output)"
            )
        refs.append(child.pane_ref)
    return refs


def validate_active_pane_width(value: object) -> int:
    """Shared validation (an integer with 0 < value < 100), used for both
    the spawn stdin JSON's explicit value and the settings file's default.
    `bool` is a subclass of `int` but is not an intended input, so it is
    explicitly rejected."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("active_pane_width must be an integer")
    if not (0 < value < 100):
        raise ValidationError("active_pane_width must be between 0 and 100 (exclusive)")
    return value


def choose_anchor(
    children: list[WorkerRecord], parent_pane_ref: str | None
) -> tuple[str, SplitDirection] | None:
    """One or more children -> split the last child's (`children[-1]`,
    last in spawn order) pane top/bottom. No children -> split the
    parent's own pane left/right. When the parent's `pane_ref` is `None`
    (the orchestrator's `$TMUX_PANE`/`$ITERM_SESSION_ID` is unset, or the
    parent worker's name cannot be resolved), returns `None` and leaves
    the fallback to the caller."""
    if children:
        last_child = children[-1]
        if last_child.pane_ref is None:
            raise ValidationError(
                f"choose_anchor: last child {last_child.name!r} has no pane_ref "
                "(caller must pass alive_pane_holders() output)"
            )
        return last_child.pane_ref, SplitDirection.VERTICAL
    if parent_pane_ref is not None:
        return parent_pane_ref, SplitDirection.HORIZONTAL
    return None

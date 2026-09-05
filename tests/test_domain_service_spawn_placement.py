from pathlib import Path

from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.domain.mapper.command_mapper import validate_name
from sukuna.domain.service.spawn_placement import (
    advance_ordinal_high_water_mark,
    children_of,
    column_of,
    existing_workers,
    is_nested_caller_pane_state,
    matches_alive_pane_ref,
    next_ordinal,
    ordinal_high_water_mark_key,
    root_children,
    worker_name,
)
from sukuna.domain.service.worker_tree import build_tree


def test_worker_name_sanitizes_a_non_ascii_repo_slug() -> None:
    name = worker_name(Path("/tmp/実験リポ"), "abc123-def", "review", 1)

    assert name == "ccw-repo-abc123-review-1"
    validate_name(name)  # must not raise


def test_worker_name_sanitizes_a_mixed_ascii_non_ascii_repo_slug() -> None:
    # Each non-ASCII character becomes its own "-" (same as any other
    # non-alnum character in the existing filter); no embedded hyphens in
    # the source name, so the two runs of replaced characters are
    # unambiguous: "my" + "--" (for 実験) + "repo".
    name = worker_name(Path("/tmp/my実験repo"), "abc123-def", "review", 1)

    assert name == "ccw-my--repo-abc123-review-1"
    validate_name(name)


def test_worker_name_falls_back_to_repo_when_slug_is_entirely_non_ascii() -> None:
    name = worker_name(Path("/tmp/実験リポ"), None, "review", 1)

    assert name == "ccw-repo-manual-review-1"
    validate_name(name)


def test_worker_name_sanitizes_and_caps_a_non_uuid_session_token() -> None:
    # No hyphen, mixed case, far longer than the 8-char cap the token is
    # kept to for a real UUID-shaped session id.
    name = worker_name(
        Path("/tmp/project"), "ANOTUUIDSESSIONIDENTIFIER120CHARSLONG", "review", 1
    )

    assert name == "ccw-project-anotuuid-review-1"
    validate_name(name)


def test_worker_name_falls_back_to_session_when_token_is_entirely_non_ascii() -> None:
    name = worker_name(Path("/tmp/project"), "実験セッション", "review", 1)

    assert name == "ccw-project-session-review-1"
    validate_name(name)


def test_worker_name_preserves_the_existing_uuid_session_token_shape() -> None:
    # Regression guard: a normal UUID-shaped session id's first segment
    # (8 hex chars) must sanitize to itself, unchanged.
    name = worker_name(Path("/tmp/project"), "af57a316-1234-5678", "review", 1)

    assert name == "ccw-project-af57a316-review-1"
    validate_name(name)


def test_root_children_agrees_with_build_tree_on_an_empty_parent_worker_name() -> None:
    """The document boundary (`WorkerRecordDocument`) rejects an empty
    `parent_worker_name` on the way in, but the dataclass itself does not
    -- constructing one directly here (as a hand-edited on-disk registry
    would produce) is the only way to exercise `root_children()` against
    this shape at all. `root_children()`'s and `build_tree()`'s
    classification of this shape must agree."""
    worker = WorkerRecord.create(
        name="ccw-project-00000000-review-1",
        repo_root="/repo",
        worktree="/repo",
        parent_session_id="orchestrator-1",
    )
    worker.parent_worker_name = ""
    worker.pane_ref = "%1"

    tree = build_tree([worker])
    roots = root_children([worker], "orchestrator-1")

    assert {child["name"] for child in tree["orchestrator-1"]} == {worker.name}
    assert roots == [worker]


def test_column_of_delegates_to_children_of_when_parent_worker_name_given() -> None:
    parent = WorkerRecord.create(
        name="ccw-project-00000000-review-1",
        repo_root="/repo",
        worktree="/repo",
        parent_session_id="orchestrator-1",
    )
    parent.pane_ref = "%1"
    child = WorkerRecord.create(
        name="ccw-project-00000000-review-2",
        repo_root="/repo",
        worktree="/repo",
        parent_session_id="orchestrator-1",
    )
    child.pane_ref = "%2"
    child.parent_worker_name = parent.name
    pane_holders = [parent, child]

    assert column_of(
        pane_holders, parent_worker_name=parent.name, parent_session_id="orchestrator-1"
    ) == children_of(pane_holders, parent.name)
    assert column_of(
        pane_holders, parent_worker_name=parent.name, parent_session_id="orchestrator-1"
    ) == [child]


def test_column_of_delegates_to_root_children_when_parent_worker_name_absent() -> None:
    root = WorkerRecord.create(
        name="ccw-project-00000000-review-1",
        repo_root="/repo",
        worktree="/repo",
        parent_session_id="orchestrator-1",
    )
    root.pane_ref = "%1"
    pane_holders = [root]

    assert column_of(
        pane_holders, parent_worker_name=None, parent_session_id="orchestrator-1"
    ) == root_children(pane_holders, "orchestrator-1")
    assert column_of(
        pane_holders, parent_worker_name=None, parent_session_id="orchestrator-1"
    ) == [root]


def test_column_of_treats_empty_parent_worker_name_as_root_like_root_children() -> None:
    """Same shape as
    `test_root_children_agrees_with_build_tree_on_an_empty_parent_worker_name`:
    an empty `parent_worker_name` (only reachable via a hand-edited on-disk
    registry, `validate_name()` rejects it on the spawn path) must classify
    as root, not as a child of the empty string."""
    worker = WorkerRecord.create(
        name="ccw-project-00000000-review-1",
        repo_root="/repo",
        worktree="/repo",
        parent_session_id="orchestrator-1",
    )
    worker.parent_worker_name = ""
    worker.pane_ref = "%1"

    assert column_of(
        [worker], parent_worker_name="", parent_session_id="orchestrator-1"
    ) == [worker]


def test_is_nested_caller_pane_state_accepts_pane_holding_states() -> None:
    """`_dispatch_focus()`'s nested-caller check treats
    READY/BUSY/REPORTED/ACCEPTED as a live pane holder."""
    for state in (
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
    ):
        assert is_nested_caller_pane_state(state) is True


def test_is_nested_caller_pane_state_excludes_stale_pane_ref_states() -> None:
    """STARTING/FAILED/TIMED_OUT/CLOSED are excluded even though
    `alive_pane_holders()` counts STARTING/FAILED/TIMED_OUT as alive --
    the two definitions are deliberately different: a FAILED/TIMED_OUT
    worker's stale `pane_ref` can collide with a different, newer
    worker's pane_ref after a tmux server restart re-numbers pane ids."""
    for state in (
        WorkerState.STARTING,
        WorkerState.FAILED,
        WorkerState.TIMED_OUT,
        WorkerState.CLOSED,
    ):
        assert is_nested_caller_pane_state(state) is False


# -- matches_alive_pane_ref --


def _worker_with_pane(*, pane_ref: str | None, state: WorkerState) -> WorkerRecord:
    worker = WorkerRecord.create(
        name="ccw-x-review-a", repo_root="/repo", worktree="/repo"
    )
    worker.pane_ref = pane_ref
    worker.state = state
    return worker


def test_matches_alive_pane_ref_true_for_a_live_state_with_the_same_pane_ref() -> None:
    worker = _worker_with_pane(pane_ref="%1", state=WorkerState.BUSY)

    assert matches_alive_pane_ref([worker], "%1") is True


def test_matches_alive_pane_ref_false_for_a_different_pane_ref() -> None:
    worker = _worker_with_pane(pane_ref="%1", state=WorkerState.BUSY)

    assert matches_alive_pane_ref([worker], "%2") is False


def test_matches_alive_pane_ref_false_for_a_stale_state_even_with_the_same_pane_ref() -> (
    None
):
    """FAILED is `alive_pane_holders()`-alive but not
    `is_nested_caller_pane_state()`-alive -- `matches_alive_pane_ref()`
    uses the latter, same as `_dispatch_focus()`."""
    worker = _worker_with_pane(pane_ref="%1", state=WorkerState.FAILED)

    assert matches_alive_pane_ref([worker], "%1") is False


def test_matches_alive_pane_ref_false_for_an_empty_record_list() -> None:
    assert matches_alive_pane_ref([], "%1") is False


# -- existing_workers --


def _make(
    name: str, *, parent_session_id: str | None, state: WorkerState
) -> WorkerRecord:
    worker = WorkerRecord.create(
        name=name,
        repo_root="/repo",
        worktree="/repo",
        parent_session_id=parent_session_id,
    )
    worker.state = state
    return worker


def test_existing_workers_filters_by_session_managed_and_excludes_closed() -> None:
    same_session = _make(
        "ccw-project-00000000-review-1",
        parent_session_id="session-a",
        state=WorkerState.READY,
    )
    other_session = _make(
        "ccw-project-00000000-review-2",
        parent_session_id="session-b",
        state=WorkerState.READY,
    )
    closed = _make(
        "ccw-project-00000000-review-3",
        parent_session_id="session-a",
        state=WorkerState.CLOSED,
    )
    unmanaged = _make(
        "ccw-project-00000000-review-4",
        parent_session_id="session-a",
        state=WorkerState.READY,
    )
    unmanaged.managed = False

    result = existing_workers(
        [same_session, other_session, closed, unmanaged], "session-a"
    )

    assert result == [same_session]


# -- ordinal_high_water_mark_key / next_ordinal --


def test_ordinal_high_water_mark_key_passes_through_a_real_session_id() -> None:
    assert ordinal_high_water_mark_key("session-abc") == "session-abc"


def test_ordinal_high_water_mark_key_sentinels_none() -> None:
    assert ordinal_high_water_mark_key(None) == "(none)"


def test_next_ordinal_uses_the_record_count_when_no_floor_is_persisted() -> None:
    records = [
        _make("ccw-a-1", parent_session_id="session-a", state=WorkerState.CLOSED),
        _make("ccw-a-2", parent_session_id="session-a", state=WorkerState.CLOSED),
    ]

    assert next_ordinal(records, {}, "session-a") == 3


def test_next_ordinal_uses_the_persisted_floor_when_it_exceeds_the_record_count() -> (
    None
):
    """The 140D89AB-reopened-by-retention scenario: records were purged, so
    the record count dropped, but the floor (seeded before that purge)
    still remembers the true high-water-mark."""
    records: list[WorkerRecord] = []

    assert next_ordinal(records, {"session-a": 5}, "session-a") == 5


def test_next_ordinal_returns_the_larger_of_the_two_lower_bounds() -> None:
    records = [
        _make("ccw-a-1", parent_session_id="session-a", state=WorkerState.CLOSED),
    ]

    assert next_ordinal(records, {"session-a": 1}, "session-a") == 2


def test_next_ordinal_scopes_the_floor_by_parent_session_id() -> None:
    records: list[WorkerRecord] = []

    assert next_ordinal(records, {"session-b": 9}, "session-a") == 1


def test_next_ordinal_handles_the_null_parent_session_id() -> None:
    records = [_make("ccw-a-1", parent_session_id=None, state=WorkerState.CLOSED)]

    assert next_ordinal(records, {"(none)": 5}, None) == 5


# -- advance_ordinal_high_water_mark --


def test_advance_ordinal_high_water_mark_sets_the_floor_to_claimed_plus_one() -> None:
    updated = advance_ordinal_high_water_mark(
        {}, parent_session_id="session-a", claimed_ordinal=1
    )

    assert updated == {"session-a": 2}


def test_advance_ordinal_high_water_mark_never_lowers_an_existing_floor() -> None:
    updated = advance_ordinal_high_water_mark(
        {"session-a": 9}, parent_session_id="session-a", claimed_ordinal=1
    )

    assert updated == {"session-a": 9}


def test_advance_ordinal_high_water_mark_does_not_mutate_the_input() -> None:
    original = {"session-a": 1}

    advance_ordinal_high_water_mark(
        original, parent_session_id="session-a", claimed_ordinal=5
    )

    assert original == {"session-a": 1}


def test_advance_ordinal_high_water_mark_handles_the_null_session() -> None:
    updated = advance_ordinal_high_water_mark(
        {}, parent_session_id=None, claimed_ordinal=0
    )

    assert updated == {"(none)": 1}

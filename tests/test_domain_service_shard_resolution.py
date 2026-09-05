"""Pure shard-key decision rules: reuse-first ROOT/child
shard resolution, migration root-walking (including orphan/cycle
pseudo-roots), the 3-tier migration fallback, and ordinal-high-water-mark
distribution across shards."""

from __future__ import annotations

from pathlib import Path

import pytest
from _terminal_fakes import make_worker as _make_worker

from sukuna.domain.service.session_log import encode_project_dir_name
from sukuna.domain.service.shard_resolution import (
    CATCH_ALL_SHARD_KEY,
    MigrationRootInfo,
    ShardSummary,
    check_cross_shard_grafting,
    distribute_ordinal_high_water_marks,
    find_cross_shard_duplicate_names,
    find_shard_for_name,
    find_shard_for_parent_session,
    migration_shard_key,
    resolve_child_shard_key,
    resolve_migration_root,
    resolve_migration_shard_keys,
    resolve_root_shard_key,
)
from sukuna.domain.service.spawn_placement import ordinal_high_water_mark_key
from sukuna.errors import ValidationError

_REPO = Path("/repo")


def test_catch_all_shard_key_never_collides_with_an_encoded_cwd_or_worktree() -> None:
    """Card's own request: `encode_project_dir_name()` only ever emits
    `[A-Za-z0-9-]`, so the parenthesized catch-all sentinel can never be
    produced by encoding a real path."""
    assert "(" not in encode_project_dir_name("/any/real/path-at-all_123")
    assert CATCH_ALL_SHARD_KEY != encode_project_dir_name("/any/real/path-at-all_123")


def test_find_shard_for_parent_session_returns_first_match_or_none() -> None:
    summaries = [
        ShardSummary(
            shard_key="a", parent_session_ids=frozenset({"s1"}), names=frozenset()
        ),
        ShardSummary(
            shard_key="b", parent_session_ids=frozenset({"s2"}), names=frozenset()
        ),
    ]
    assert find_shard_for_parent_session(summaries, "s2") == "b"
    assert find_shard_for_parent_session(summaries, "unknown") is None


def test_find_shard_for_name_returns_first_match_or_none() -> None:
    summaries = [
        ShardSummary(
            shard_key="a", parent_session_ids=frozenset(), names=frozenset({"w1"})
        ),
        ShardSummary(
            shard_key="b", parent_session_ids=frozenset(), names=frozenset({"w2"})
        ),
    ]
    assert find_shard_for_name(summaries, "w2") == "b"
    assert find_shard_for_name(summaries, "w3") is None


def test_resolve_root_shard_key_reuses_an_existing_shard_over_cwd() -> None:
    """Mandatory per the card: a session that already has a shard must keep
    using it, even if `cwd_shard_key` differs -- this is what prevents a
    same-session, cross-cwd ordinal collision."""
    summaries = [
        ShardSummary(
            shard_key="existing",
            parent_session_ids=frozenset({"s1"}),
            names=frozenset(),
        )
    ]

    assert (
        resolve_root_shard_key(
            summaries, parent_session_id="s1", cwd_shard_key="fresh-cwd-key"
        )
        == "existing"
    )


def test_resolve_root_shard_key_falls_back_to_cwd_when_session_is_new() -> None:
    assert (
        resolve_root_shard_key(
            [], parent_session_id="s1", cwd_shard_key="fresh-cwd-key"
        )
        == "fresh-cwd-key"
    )


def test_resolve_root_shard_key_treats_none_session_like_any_other_value() -> None:
    """No special-casing for `parent_session_id=None` (manual/no-session
    spawns): reuse-first applies uniformly, matching the pre-sharding
    world where all such records already lived together in one file."""
    summaries = [
        ShardSummary(
            shard_key="(legacy)",
            parent_session_ids=frozenset({None}),
            names=frozenset(),
        )
    ]

    assert (
        resolve_root_shard_key(
            summaries, parent_session_id=None, cwd_shard_key="cwd-key"
        )
        == "(legacy)"
    )


def test_resolve_child_shard_key_inherits_the_named_parents_shard() -> None:
    summaries = [
        ShardSummary(
            shard_key="parent-shard",
            parent_session_ids=frozenset(),
            names=frozenset({"ccw-p"}),
        )
    ]

    assert (
        resolve_child_shard_key(
            summaries,
            parent_worker_name="ccw-p",
            parent_session_id="irrelevant",
            cwd_shard_key="cwd-key",
        )
        == "parent-shard"
    )


def test_resolve_child_shard_key_falls_back_to_root_rule_when_parent_unresolvable() -> (
    None
):
    """Typo, closed, or never-existed parent name: same fallback the
    placement resolver (`usecase/spawn_shared.py`) already applies for
    anchor selection."""
    summaries = [
        ShardSummary(
            shard_key="unrelated",
            parent_session_ids=frozenset({"other-session"}),
            names=frozenset(),
        )
    ]

    assert (
        resolve_child_shard_key(
            summaries,
            parent_worker_name="ccw-does-not-exist",
            parent_session_id="s1",
            cwd_shard_key="cwd-key",
        )
        == "cwd-key"
    )


def test_check_cross_shard_grafting_raises_when_own_shard_differs_from_parents() -> (
    None
):
    """Cross-shard grafting guard: the calling session *already has records* in
    "caller-shard" (`find_shard_for_parent_session()` finds it), which
    differs from the named parent's actual shard ("parent-shard") -- must
    raise. This is the exact precondition for the name/ordinal collision
    the guard exists to prevent (a session's records split across two
    shards)."""
    summaries = [
        ShardSummary(
            shard_key="parent-shard",
            parent_session_ids=frozenset({"root-session"}),
            names=frozenset({"ccw-p"}),
        ),
        ShardSummary(
            shard_key="caller-shard",
            parent_session_ids=frozenset({"caller-session"}),
            names=frozenset(),
        ),
    ]

    with pytest.raises(ValidationError):
        check_cross_shard_grafting(
            summaries,
            parent_worker_name="ccw-p",
            parent_session_id="caller-session",
        )


def test_check_cross_shard_grafting_allows_reuse_first_match() -> None:
    """The calling session already owns the parent's shard (reuse-first
    finds it via `parent_session_id`) -- no-op, no raise."""
    summaries = [
        ShardSummary(
            shard_key="shared-shard",
            parent_session_ids=frozenset({"same-session"}),
            names=frozenset({"ccw-p"}),
        )
    ]

    check_cross_shard_grafting(
        summaries,
        parent_worker_name="ccw-p",
        parent_session_id="same-session",
    )


def test_check_cross_shard_grafting_allows_a_session_with_no_existing_shard() -> None:
    """Corrected round-4 condition: a calling session with zero records
    anywhere (`find_shard_for_parent_session()` returns `None`) is always
    allowed, regardless of cwd -- this is the standard documented
    grandchild flow (a worker spawning under its own name from a fresh
    `$CLAUDE_CODE_SESSION_ID` it has never spawned under before). No-op,
    no raise."""
    summaries = [
        ShardSummary(
            shard_key="parent-shard",
            parent_session_ids=frozenset({"root-session"}),
            names=frozenset({"ccw-p"}),
        )
    ]

    check_cross_shard_grafting(
        summaries,
        parent_worker_name="ccw-p",
        parent_session_id="caller-session-with-no-records-anywhere",
    )


def test_check_cross_shard_grafting_is_a_noop_for_an_unresolvable_parent() -> None:
    """Typo/closed/nonexistent `parent_worker_name`: the existing
    ROOT-rule fallback applies elsewhere, so the guard must not fire --
    there is no *other* shard here to conflict with."""
    summaries = [
        ShardSummary(
            shard_key="unrelated", parent_session_ids=frozenset(), names=frozenset()
        )
    ]

    check_cross_shard_grafting(
        summaries,
        parent_worker_name="ccw-does-not-exist",
        parent_session_id="caller-session",
    )


def test_resolve_migration_root_returns_the_true_root_when_chain_terminates() -> None:
    root = _make_worker(
        _REPO, name="ccw-root", parent_session_id="orchestrator-session"
    )
    child = _make_worker(
        _REPO,
        name="ccw-child",
        parent_session_id="root-own-session",
        parent_worker_name="ccw-root",
    )
    grandchild = _make_worker(
        _REPO,
        name="ccw-grandchild",
        parent_session_id="child-own-session",
        parent_worker_name="ccw-child",
    )
    by_name = {w.name: w for w in (root, child, grandchild)}

    info = resolve_migration_root(grandchild, by_name)

    assert info == MigrationRootInfo(
        parent_session_id="orchestrator-session", worktree="/repo"
    )


def test_resolve_migration_root_stops_at_an_orphan() -> None:
    orphan_child = _make_worker(
        _REPO,
        name="ccw-orphan-child",
        parent_session_id="own-session",
        parent_worker_name="ccw-does-not-exist",
    )
    by_name = {orphan_child.name: orphan_child}

    info = resolve_migration_root(orphan_child, by_name)

    # The orphan record itself is the pseudo-root -- its own fields, not a
    # reachable ancestor's (there is none).
    assert info == MigrationRootInfo(parent_session_id="own-session", worktree="/repo")


def test_resolve_migration_root_stops_at_a_two_node_cycle() -> None:
    a = _make_worker(
        _REPO, name="ccw-a", parent_session_id="a-session", parent_worker_name="ccw-b"
    )
    b = _make_worker(
        _REPO, name="ccw-b", parent_session_id="b-session", parent_worker_name="ccw-a"
    )
    by_name = {a.name: a, b.name: b}

    # Walking from `a`: a -> b (seen={a,b}) -> a already seen -> stop at b.
    info = resolve_migration_root(a, by_name)

    assert info == MigrationRootInfo(parent_session_id="b-session", worktree="/repo")


def test_resolve_migration_root_stops_at_a_self_cycle() -> None:
    a = _make_worker(
        _REPO, name="ccw-a", parent_session_id="a-session", parent_worker_name="ccw-a"
    )
    by_name = {a.name: a}

    info = resolve_migration_root(a, by_name)

    assert info == MigrationRootInfo(parent_session_id="a-session", worktree="/repo")


def test_migration_shard_key_tier1_wins_when_transcript_is_known() -> None:
    info = MigrationRootInfo(parent_session_id="s1", worktree="/some/other/worktree")

    key = migration_shard_key(
        info, transcript_shard_by_session_id={"s1": "-transcript-encoded-dir"}
    )

    assert key == "-transcript-encoded-dir"


def test_migration_shard_key_tier2_falls_back_to_encoded_worktree() -> None:
    info = MigrationRootInfo(parent_session_id="s1", worktree="/a/worktree")

    key = migration_shard_key(info, transcript_shard_by_session_id={})

    assert key == encode_project_dir_name("/a/worktree")


def test_migration_shard_key_tier3_catch_all_for_none_session() -> None:
    info = MigrationRootInfo(parent_session_id=None, worktree="/a/worktree")

    key = migration_shard_key(info, transcript_shard_by_session_id={})

    assert key == CATCH_ALL_SHARD_KEY


def test_distribute_ordinal_high_water_marks_routes_each_key_to_its_records_shard() -> (
    None
):
    root_a = _make_worker(_REPO, name="ccw-root-a", parent_session_id="session-a")
    root_b = _make_worker(_REPO, name="ccw-root-b", parent_session_id="session-b")
    records = [root_a, root_b]
    shard_key_by_name = {"ccw-root-a": "shard-a", "ccw-root-b": "shard-b"}
    hwm = {
        ordinal_high_water_mark_key("session-a"): 3,
        ordinal_high_water_mark_key("session-b"): 7,
    }

    result = distribute_ordinal_high_water_marks(
        hwm, shard_key_by_name=shard_key_by_name, records=records
    )

    assert result == {"shard-a": {"session-a": 3}, "shard-b": {"session-b": 7}}


def test_distribute_ordinal_high_water_marks_drops_a_zero_record_key() -> None:
    """A floor whose every record was already purged before migration has
    no shard to route to -- documented, low-risk drop (card: 98% of
    pre-migration records are already CLOSED)."""
    hwm = {"already-purged-session": 5}

    result = distribute_ordinal_high_water_marks(hwm, shard_key_by_name={}, records=[])

    assert result == {}


def test_resolve_migration_shard_keys_keeps_one_session_together_despite_a_tier2_worktree_split() -> (
    None
):
    """Advisor-flagged correctness gap: a session that ROOT-spawned into
    two different worktrees, with no transcript for either (Tier 1 miss),
    must resolve to exactly one shard -- otherwise that session's ordinal
    accounting splits across shards, reopening the 140D89AB/06DBAF3F
    collision class."""
    root_a = MigrationRootInfo(parent_session_id="session-s", worktree="/worktree-a")
    root_b = MigrationRootInfo(parent_session_id="session-s", worktree="/worktree-b")
    root_infos = {"ccw-root-a": root_a, "ccw-root-b": root_b}

    keys = resolve_migration_shard_keys(root_infos, transcript_shard_by_session_id={})

    assert keys["ccw-root-a"] == keys["ccw-root-b"]
    # Deterministic: the record-name-sorted-first member's worktree wins.
    assert keys["ccw-root-a"] == encode_project_dir_name("/worktree-a")


def test_resolve_migration_shard_keys_tier1_still_wins_per_session() -> None:
    root_a = MigrationRootInfo(parent_session_id="session-s", worktree="/worktree-a")
    root_b = MigrationRootInfo(parent_session_id="session-s", worktree="/worktree-b")
    root_infos = {"ccw-root-a": root_a, "ccw-root-b": root_b}

    keys = resolve_migration_shard_keys(
        root_infos, transcript_shard_by_session_id={"session-s": "-transcript-dir"}
    )

    assert keys == {"ccw-root-a": "-transcript-dir", "ccw-root-b": "-transcript-dir"}


def test_resolve_migration_shard_keys_does_not_group_none_sessions() -> None:
    """`None` needs no grouping: Tier 3 always routes it to the catch-all
    regardless of `worktree`, so two unrelated `None`-session records
    trivially land in the same (correct) place without special-casing."""
    root_infos = {
        "ccw-a": MigrationRootInfo(parent_session_id=None, worktree="/a"),
        "ccw-b": MigrationRootInfo(parent_session_id=None, worktree="/b"),
    }

    keys = resolve_migration_shard_keys(root_infos, transcript_shard_by_session_id={})

    assert keys == {"ccw-a": CATCH_ALL_SHARD_KEY, "ccw-b": CATCH_ALL_SHARD_KEY}


def test_find_cross_shard_duplicate_names_flags_a_name_in_two_shards() -> None:
    """Layer 2: `reconcile`'s cross-shard duplicate-name
    report. A name present in two shards' record lists is flagged; each
    shard's own copy of the record is preserved in `entries`, paired with
    the shard key it came from."""
    dup_a = _make_worker(_REPO, name="ccw-dup")
    dup_b = _make_worker(_REPO, name="ccw-dup")
    unique = _make_worker(_REPO, name="ccw-unique")

    groups = find_cross_shard_duplicate_names(
        [("shard-a", [dup_a, unique]), ("shard-b", [dup_b])]
    )

    assert len(groups) == 1
    (group,) = groups
    assert group.name == "ccw-dup"
    assert group.entries == (("shard-a", dup_a), ("shard-b", dup_b))


def test_find_cross_shard_duplicate_names_is_empty_when_every_name_is_unique() -> None:
    groups = find_cross_shard_duplicate_names(
        [
            ("shard-a", [_make_worker(_REPO, name="ccw-a")]),
            ("shard-b", [_make_worker(_REPO, name="ccw-b")]),
        ]
    )

    assert groups == []

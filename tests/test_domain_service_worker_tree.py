from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict, Unpack

from _terminal_fakes import make_worker as _make_worker

from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.domain.service.retention import sweep_stale_records
from sukuna.domain.service.worker_tree import (
    attach_resumable_flags,
    attach_session_log_paths,
    build_tree,
    find_deepest_busy,
    prune_to_resumable,
    resolve_group_session_log_paths,
    select_parent_groups,
)


class _WorkerOverrides(TypedDict, total=False):
    pane_ref: str | None
    suffix: str | None
    goal: str | None
    parent_worker_name: str | None
    worktree: Path | None


def make_worker(
    repo: Path, *, parent_session_id: str | None, **overrides: Unpack[_WorkerOverrides]
) -> WorkerRecord:
    return _make_worker(repo, parent_session_id=parent_session_id, **overrides)


def test_build_tree_groups_workers_by_their_spawning_session(tmp_path: Path) -> None:
    child_a = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    child_b = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="b")
    other_child = make_worker(tmp_path, parent_session_id="orchestrator-2", suffix="c")
    unattributed = make_worker(tmp_path, parent_session_id=None, suffix="d")

    tree = build_tree([child_a, child_b, other_child, unattributed])

    assert {child["name"] for child in tree["orchestrator-1"]} == {
        child_a.name,
        child_b.name,
    }
    assert {child["name"] for child in tree["orchestrator-2"]} == {other_child.name}
    assert {child["name"] for child in tree["(unknown parent)"]} == {unattributed.name}


def test_build_tree_nests_grandworkers_via_parent_worker_name(tmp_path: Path) -> None:
    worker_a = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    worker_b = make_worker(
        tmp_path, parent_session_id=None, suffix="b", parent_worker_name=worker_a.name
    )
    worker_c = make_worker(
        tmp_path, parent_session_id=None, suffix="c", parent_worker_name=worker_b.name
    )

    tree = build_tree([worker_a, worker_b, worker_c])

    (node_a,) = tree["orchestrator-1"]
    assert node_a["name"] == worker_a.name
    (node_b,) = node_a["children"]
    assert node_b["name"] == worker_b.name
    (node_c,) = node_b["children"]
    assert node_c["name"] == worker_c.name
    assert node_c["children"] == []


def test_build_tree_shows_orphaned_workers_whose_parent_is_missing(
    tmp_path: Path,
) -> None:
    worker = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="a",
        parent_worker_name="ccw-ghost-parent-1",
    )

    tree = build_tree([worker])

    key = "(orphaned: parent 'ccw-ghost-parent-1' not found)"
    assert {child["name"] for child in tree[key]} == {worker.name}


def test_build_tree_shows_retention_purged_parents_children_as_orphaned(
    tmp_path: Path,
) -> None:
    """Card 06DBAF3F's tree-side acceptance criterion: a parent purged by
    `retention.sweep_stale_records()` (old CLOSED/FAILED, dropped from the
    registry entirely) whose still-alive child survives the sweep must
    surface in `build_tree()`'s existing `"(orphaned: ...)"` bucket --
    `_orphan_groups()` already handles "parent not found" generically
    (manual close, retention purge, or anything else that removes a
    record), so no new tree code path is needed; this pins that composition
    of the two pure functions."""
    now = datetime(2026, 8, 30, tzinfo=UTC)
    parent = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="parent")
    parent.state = WorkerState.CLOSED
    parent.updated_at = (now - timedelta(days=31)).isoformat()
    child = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="child",
        parent_worker_name=parent.name,
    )
    child.state = WorkerState.CLOSED
    child.updated_at = now.isoformat()

    _, survivors, purged = sweep_stale_records(
        "registry.json", [parent, child], retention_days=30, now=now
    )
    assert [record.name for record in purged] == [parent.name]

    tree = build_tree(survivors)

    key = f"(orphaned: parent '{parent.name}' not found)"
    assert {node["name"] for node in tree[key]} == {child.name}


def test_build_tree_surfaces_an_isolated_parent_cycle_unreachable_from_any_root(
    tmp_path: Path,
) -> None:
    worker_b = make_worker(tmp_path, parent_session_id=None, suffix="b")
    worker_a = make_worker(
        tmp_path, parent_session_id=None, suffix="a", parent_worker_name=worker_b.name
    )
    worker_b.parent_worker_name = worker_a.name

    tree = build_tree([worker_a, worker_b])

    key = "(cycle: unreachable from any root)"
    assert set(tree.keys()) == {key}
    rendered = str(tree[key])
    assert worker_a.name in rendered
    assert worker_b.name in rendered
    assert "cycle detected" in rendered


def test_build_tree_includes_a_worker_that_never_got_a_pane(tmp_path: Path) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    worker.transition_to(WorkerState.FAILED)

    tree = build_tree([worker])

    assert tree["orchestrator-1"][0]["state"] == "failed"


def test_build_tree_shows_each_workers_goal(tmp_path: Path) -> None:
    worker = make_worker(
        tmp_path, parent_session_id="orchestrator-1", suffix="a", goal="find the leak"
    )

    tree = build_tree([worker])

    assert tree["orchestrator-1"][0]["goal"] == "find the leak"


def test_build_tree_sorts_the_orphan_bucket_by_updated_at_ascending(
    tmp_path: Path,
) -> None:
    newer = make_worker(
        tmp_path, parent_session_id=None, suffix="a", parent_worker_name="ghost"
    )
    older = make_worker(
        tmp_path, parent_session_id=None, suffix="b", parent_worker_name="ghost"
    )
    newer.updated_at = "2026-08-22T00:00:00+00:00"
    older.updated_at = "2026-08-20T00:00:00+00:00"

    tree = build_tree([newer, older])

    key = "(orphaned: parent 'ghost' not found)"
    assert [node["name"] for node in tree[key]] == [older.name, newer.name]


def test_build_tree_keeps_the_cycle_bucket_sorted_by_updated_at_ascending(
    tmp_path: Path,
) -> None:
    early_a = make_worker(tmp_path, parent_session_id=None, suffix="early-a")
    early_b = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="early-b",
        parent_worker_name=early_a.name,
    )
    early_a.parent_worker_name = early_b.name
    early_a.updated_at = "2026-08-20T00:00:00+00:00"
    early_b.updated_at = "2026-08-20T00:00:01+00:00"

    late_a = make_worker(tmp_path, parent_session_id=None, suffix="late-a")
    late_b = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="late-b",
        parent_worker_name=late_a.name,
    )
    late_a.parent_worker_name = late_b.name
    late_a.updated_at = "2026-08-22T00:00:00+00:00"
    late_b.updated_at = "2026-08-22T00:00:01+00:00"

    tree = build_tree([late_a, late_b, early_a, early_b])

    key = "(cycle: unreachable from any root)"
    top_level_names = [node["name"] for node in tree[key]]
    assert top_level_names == [early_a.name, late_a.name]


def _contains_a_string_marker(items: list[Any]) -> bool:
    for item in items:
        if isinstance(item, str):
            return True
        if _contains_a_string_marker(item["children"]):
            return True
    return False


def _collect_node_names(tree: dict[str, list[Any]]) -> list[str]:
    names: list[str] = []

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if isinstance(node, str):
                continue
            names.append(node["name"])
            walk(node["children"])

    for nodes in tree.values():
        walk(nodes)
    return names


def test_build_tree_renders_a_cycles_dangling_descendant_exactly_once(
    tmp_path: Path,
) -> None:
    """A normal child (C) hanging off a cycle member (A, in an A<->B cycle)
    must not be double-rendered -- once as a top-level "unreachable" entry
    (because its own `updated_at` sorts before the cycle members') and
    once again as A's child via recursion."""
    worker_a = make_worker(tmp_path, parent_session_id=None, suffix="a")
    worker_b = make_worker(
        tmp_path, parent_session_id=None, suffix="b", parent_worker_name=worker_a.name
    )
    worker_a.parent_worker_name = worker_b.name
    worker_a.updated_at = "2026-08-21T00:00:00+00:00"
    worker_b.updated_at = "2026-08-21T00:00:01+00:00"

    worker_c = make_worker(
        tmp_path, parent_session_id=None, suffix="c", parent_worker_name=worker_a.name
    )
    worker_c.updated_at = "2026-08-20T00:00:00+00:00"  # oldest -- sorts first

    tree = build_tree([worker_a, worker_b, worker_c])

    key = "(cycle: unreachable from any root)"
    assert set(tree.keys()) == {key}
    names = _collect_node_names(tree)
    assert sorted(names) == sorted([worker_a.name, worker_b.name, worker_c.name])
    assert len(names) == 3

    (top_node,) = tree[key]
    assert top_node["name"] == worker_a.name
    child_names = {
        child["name"] for child in top_node["children"] if not isinstance(child, str)
    }
    assert worker_c.name in child_names


def test_build_tree_sorts_children_by_true_time_order_across_mixed_utc_offsets(
    tmp_path: Path,
) -> None:
    """A raw ISO-8601 string comparison only agrees with chronological
    order when every value shares the same UTC offset. `earlier` (01:00
    UTC, written as +09:00) sorts after `later` (01:30 UTC, written as
    +00:00) as plain strings ("10:00..." > "01:30...") but is
    chronologically first -- `build_tree()` must sort by parsed datetime,
    not by the raw string, to get this right."""
    later = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="later")
    earlier = make_worker(
        tmp_path, parent_session_id="orchestrator-1", suffix="earlier"
    )
    later.updated_at = "2026-08-27T01:30:00+00:00"
    earlier.updated_at = "2026-08-27T10:00:00+09:00"
    assert earlier.updated_at > later.updated_at  # wrong order as raw strings

    tree = build_tree([later, earlier])

    assert [node["name"] for node in tree["orchestrator-1"]] == [
        earlier.name,
        later.name,
    ]


def test_select_parent_groups_shows_all_groups_when_count_equals_limit(
    tmp_path: Path,
) -> None:
    workers = [
        make_worker(tmp_path, parent_session_id=f"orchestrator-{i}", suffix=f"a{i}")
        for i in range(3)
    ]
    for index, worker in enumerate(workers):
        worker.updated_at = f"2026-08-2{index}T00:00:00+00:00"

    selected, hidden = select_parent_groups(build_tree(workers), limit=3)

    assert hidden == 0
    assert set(selected.keys()) == {
        "orchestrator-0",
        "orchestrator-1",
        "orchestrator-2",
    }


def test_select_parent_groups_hides_the_oldest_groups_past_the_limit(
    tmp_path: Path,
) -> None:
    workers = [
        make_worker(tmp_path, parent_session_id=f"orchestrator-{i}", suffix=f"a{i}")
        for i in range(4)
    ]
    for index, worker in enumerate(workers):
        worker.updated_at = f"2026-08-2{index}T00:00:00+00:00"

    selected, hidden = select_parent_groups(build_tree(workers), limit=3)

    assert hidden == 1
    assert "orchestrator-0" not in selected
    assert set(selected.keys()) == {
        "orchestrator-1",
        "orchestrator-2",
        "orchestrator-3",
    }


def test_select_parent_groups_with_zero_limit_hides_every_group(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")

    selected, hidden = select_parent_groups(build_tree([worker]), limit=0)

    assert selected == {}
    assert hidden == 1


def test_select_parent_groups_with_none_limit_keeps_every_group(
    tmp_path: Path,
) -> None:
    """`limit=None` is the TUI's unlimited mode -- more groups than the
    CLI's default limit of 5 must all survive."""
    workers = [
        make_worker(tmp_path, parent_session_id=f"orchestrator-{i}", suffix=f"a{i}")
        for i in range(6)
    ]
    for index, worker in enumerate(workers):
        worker.updated_at = f"2026-08-2{index}T00:00:00+00:00"

    selected, hidden = select_parent_groups(build_tree(workers), limit=None)

    assert hidden == 0
    assert set(selected.keys()) == {f"orchestrator-{i}" for i in range(6)}


def test_select_parent_groups_orders_selected_groups_by_representative_updated_at_descending(
    tmp_path: Path,
) -> None:
    workers = [
        make_worker(tmp_path, parent_session_id=f"orchestrator-{i}", suffix=f"a{i}")
        for i in range(3)
    ]
    for index, worker in enumerate(workers):
        worker.updated_at = f"2026-08-2{index}T00:00:00+00:00"

    selected, _hidden = select_parent_groups(build_tree(workers), limit=3)

    assert list(selected.keys()) == [
        "orchestrator-2",
        "orchestrator-1",
        "orchestrator-0",
    ]


def test_select_parent_groups_since_and_to_are_inclusive_at_the_boundary(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    worker.updated_at = "2026-08-20T00:00:00+00:00"
    boundary = datetime.fromisoformat("2026-08-20T00:00:00+00:00")

    since_selected, since_hidden = select_parent_groups(
        build_tree([worker]), limit=5, since=boundary
    )
    to_selected, to_hidden = select_parent_groups(
        build_tree([worker]), limit=5, to=boundary
    )

    assert "orchestrator-1" in since_selected
    assert since_hidden == 0
    assert "orchestrator-1" in to_selected
    assert to_hidden == 0


def test_select_parent_groups_excludes_groups_outside_since_to_range(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    worker.updated_at = "2026-08-20T00:00:00+00:00"

    too_early_selected, too_early_hidden = select_parent_groups(
        build_tree([worker]),
        limit=5,
        since=datetime.fromisoformat("2026-08-21T00:00:00+00:00"),
    )
    too_late_selected, too_late_hidden = select_parent_groups(
        build_tree([worker]),
        limit=5,
        to=datetime.fromisoformat("2026-08-19T00:00:00+00:00"),
    )

    assert too_early_selected == {}
    assert too_early_hidden == 0
    assert too_late_selected == {}
    assert too_late_hidden == 0


def test_select_parent_groups_representative_is_the_groups_latest_member(
    tmp_path: Path,
) -> None:
    older_group = [
        make_worker(tmp_path, parent_session_id="orchestrator-old", suffix=f"o{i}")
        for i in range(2)
    ]
    older_group[0].updated_at = "2026-08-01T00:00:00+00:00"
    older_group[1].updated_at = "2026-08-02T00:00:00+00:00"
    newer_group = [
        make_worker(tmp_path, parent_session_id="orchestrator-new", suffix=f"n{i}")
        for i in range(2)
    ]
    newer_group[0].updated_at = "2026-08-10T00:00:00+00:00"
    newer_group[1].updated_at = "2026-08-11T00:00:00+00:00"

    selected, hidden = select_parent_groups(
        build_tree([*older_group, *newer_group]), limit=1
    )

    assert hidden == 1
    assert list(selected.keys()) == ["orchestrator-new"]


def test_select_parent_groups_treats_special_buckets_as_parent_groups(
    tmp_path: Path,
) -> None:
    session_worker = make_worker(
        tmp_path, parent_session_id="orchestrator-1", suffix="s"
    )
    session_worker.updated_at = "2026-08-01T00:00:00+00:00"
    orphan_worker = make_worker(
        tmp_path, parent_session_id=None, suffix="o", parent_worker_name="ghost"
    )
    orphan_worker.updated_at = "2026-08-20T00:00:00+00:00"

    selected, hidden = select_parent_groups(
        build_tree([session_worker, orphan_worker]), limit=1
    )

    assert hidden == 1
    assert list(selected.keys()) == ["(orphaned: parent 'ghost' not found)"]


def test_select_parent_groups_does_not_alter_a_selected_groups_nodes(
    tmp_path: Path,
) -> None:
    root = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="root")
    children = [
        make_worker(
            tmp_path,
            parent_session_id=None,
            suffix=f"c{i}",
            parent_worker_name=root.name,
        )
        for i in range(6)
    ]
    for index, child in enumerate(children):
        child.updated_at = f"2026-08-0{index + 1}T00:00:00+00:00"
    original = build_tree([root, *children])

    selected, hidden = select_parent_groups(original, limit=5)

    assert hidden == 0
    assert selected["orchestrator-1"] is original["orchestrator-1"]
    (node,) = selected["orchestrator-1"]
    assert len(node["children"]) == 6


def test_find_deepest_busy_returns_none_when_nothing_is_busy(tmp_path: Path) -> None:
    idle = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")

    tree = build_tree([idle])

    assert find_deepest_busy(tree, "orchestrator-1") is None


def test_find_deepest_busy_returns_none_for_an_absent_root_key(tmp_path: Path) -> None:
    busy = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    busy.state = WorkerState.BUSY

    tree = build_tree([busy])

    assert find_deepest_busy(tree, "orchestrator-2") is None


def test_find_deepest_busy_prefers_depth_over_a_more_recent_updated_at(
    tmp_path: Path,
) -> None:
    """Dispatching a shallow child to BUSY must not steal focus from a
    deeper grandchild that has been BUSY for longer -- depth is the
    primary key, `updated_at` only breaks ties at equal depth."""
    child_a = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    child_a.state = WorkerState.BUSY
    child_a.updated_at = "2026-08-25T00:00:05+00:00"  # just became busy (newest)

    grandchild_b = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="b",
        parent_worker_name=child_a.name,
    )
    grandchild_b.state = WorkerState.BUSY
    grandchild_b.updated_at = "2026-08-25T00:00:01+00:00"  # busy for longer (older)

    tree = build_tree([child_a, grandchild_b])

    assert find_deepest_busy(tree, "orchestrator-1") == grandchild_b.name


def test_find_deepest_busy_breaks_ties_at_equal_depth_by_most_recent_updated_at(
    tmp_path: Path,
) -> None:
    sibling_a = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    sibling_a.state = WorkerState.BUSY
    sibling_a.updated_at = "2026-08-25T00:00:01+00:00"

    sibling_b = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="b")
    sibling_b.state = WorkerState.BUSY
    sibling_b.updated_at = "2026-08-25T00:00:02+00:00"

    tree = build_tree([sibling_a, sibling_b])

    assert find_deepest_busy(tree, "orchestrator-1") == sibling_b.name


def test_find_deepest_busy_breaks_ties_by_true_time_order_across_mixed_utc_offsets(
    tmp_path: Path,
) -> None:
    """Same mixed-offset scenario as
    `test_build_tree_sorts_children_by_true_time_order_across_mixed_utc_offsets`,
    applied to `find_deepest_busy`'s equal-depth tie-break."""
    later = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="later")
    later.state = WorkerState.BUSY
    later.updated_at = "2026-08-27T01:30:00+00:00"

    earlier = make_worker(
        tmp_path, parent_session_id="orchestrator-1", suffix="earlier"
    )
    earlier.state = WorkerState.BUSY
    earlier.updated_at = "2026-08-27T10:00:00+09:00"
    assert earlier.updated_at > later.updated_at  # wrong order as raw strings

    tree = build_tree([later, earlier])

    assert find_deepest_busy(tree, "orchestrator-1") == later.name


def test_find_deepest_busy_skips_cycle_detected_string_markers(tmp_path: Path) -> None:
    """`build_tree()` renders a cycle-detected child as a plain string, never
    a node dict (worker_tree.py:50-54) -- `find_deepest_busy` must not treat
    it as a candidate. early_b, the BUSY member one hop deeper than early_a,
    must still be found past the string marker nested under it."""
    early_a = make_worker(tmp_path, parent_session_id=None, suffix="early-a")
    early_b = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="early-b",
        parent_worker_name=early_a.name,
    )
    early_a.parent_worker_name = early_b.name
    early_b.state = WorkerState.BUSY

    tree = build_tree([early_a, early_b])

    key = "(cycle: unreachable from any root)"
    assert find_deepest_busy(tree, key) == early_b.name


def test_attach_session_log_paths_calls_resolve_with_worktree_and_name(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    tree = build_tree([worker])
    calls: list[tuple[str, str]] = []

    def fake_resolve(worktree: str, name: str) -> str | None:
        calls.append((worktree, name))
        return f"/logs/{name}.jsonl"

    attach_session_log_paths(tree, {worker.name: str(tmp_path)}, fake_resolve)

    (node,) = tree["orchestrator-1"]
    assert node["session_log_path"] == f"/logs/{worker.name}.jsonl"
    assert calls == [(str(tmp_path), worker.name)]


def test_attach_session_log_paths_sets_none_when_worktree_is_unknown(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    tree = build_tree([worker])

    attach_session_log_paths(tree, {}, lambda worktree, name: "/should-not-be-called")

    (node,) = tree["orchestrator-1"]
    assert node["session_log_path"] is None


def test_attach_session_log_paths_recurses_into_nested_children(
    tmp_path: Path,
) -> None:
    parent = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="parent")
    child = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="child",
        parent_worker_name=parent.name,
    )
    tree = build_tree([parent, child])

    attach_session_log_paths(
        tree,
        {parent.name: str(tmp_path), child.name: str(tmp_path)},
        lambda worktree, name: f"/logs/{name}.jsonl",
    )

    (parent_node,) = tree["orchestrator-1"]
    (child_node,) = parent_node["children"]
    assert child_node["session_log_path"] == f"/logs/{child.name}.jsonl"


def test_attach_session_log_paths_skips_cycle_marker_strings(tmp_path: Path) -> None:
    worker_a = make_worker(tmp_path, parent_session_id=None, suffix="a")
    worker_b = make_worker(
        tmp_path, parent_session_id=None, suffix="b", parent_worker_name=worker_a.name
    )
    worker_a.parent_worker_name = worker_b.name
    tree = build_tree([worker_a, worker_b])

    attach_session_log_paths(
        tree,
        {worker_a.name: str(tmp_path), worker_b.name: str(tmp_path)},
        lambda worktree, name: f"/logs/{name}.jsonl",
    )

    key = "(cycle: unreachable from any root)"
    (node,) = tree[key]
    # the cycle-detected string marker lands one level deeper than `node`
    # itself (repr -> cycle partner -> string marker back to repr), same
    # shape asserted via `str(tree[key])` in
    # test_build_tree_surfaces_an_isolated_parent_cycle_unreachable_from_any_root.
    assert _contains_a_string_marker(node["children"])


def test_attach_resumable_flags_true_when_worktree_matches_cwd(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    tree = build_tree([worker])

    attach_resumable_flags(tree, {worker.name: worker}, str(tmp_path))

    (node,) = tree["orchestrator-1"]
    assert node["resumable"] is True


def test_attach_resumable_flags_false_when_worktree_differs_from_cwd(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    tree = build_tree([worker])

    attach_resumable_flags(tree, {worker.name: worker}, str(tmp_path / "elsewhere"))

    (node,) = tree["orchestrator-1"]
    assert node["resumable"] is False


def test_attach_resumable_flags_recurses_into_nested_children(tmp_path: Path) -> None:
    parent = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="parent")
    child = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="child",
        parent_worker_name=parent.name,
        worktree=tmp_path / "elsewhere",
    )
    tree = build_tree([parent, child])

    attach_resumable_flags(
        tree, {parent.name: parent, child.name: child}, str(tmp_path)
    )

    (parent_node,) = tree["orchestrator-1"]
    (child_node,) = parent_node["children"]
    assert parent_node["resumable"] is True
    assert child_node["resumable"] is False


def test_attach_resumable_flags_skips_cycle_marker_strings(tmp_path: Path) -> None:
    worker_a = make_worker(tmp_path, parent_session_id=None, suffix="a")
    worker_b = make_worker(
        tmp_path, parent_session_id=None, suffix="b", parent_worker_name=worker_a.name
    )
    worker_a.parent_worker_name = worker_b.name
    tree = build_tree([worker_a, worker_b])

    attach_resumable_flags(
        tree, {worker_a.name: worker_a, worker_b.name: worker_b}, str(tmp_path)
    )

    key = "(cycle: unreachable from any root)"
    (node,) = tree[key]
    assert _contains_a_string_marker(node["children"])


def test_prune_to_resumable_drops_a_non_resumable_leaf(tmp_path: Path) -> None:
    resumable = make_worker(
        tmp_path, parent_session_id="orchestrator-1", suffix="resumable"
    )
    stale = make_worker(
        tmp_path,
        parent_session_id="orchestrator-1",
        suffix="stale",
        worktree=tmp_path / "elsewhere",
    )
    tree = build_tree([resumable, stale])
    attach_resumable_flags(
        tree, {resumable.name: resumable, stale.name: stale}, str(tmp_path)
    )

    pruned = prune_to_resumable(tree)

    assert {node["name"] for node in pruned["orchestrator-1"]} == {resumable.name}


def test_prune_to_resumable_drops_a_group_left_entirely_empty(tmp_path: Path) -> None:
    stale = make_worker(
        tmp_path,
        parent_session_id="orchestrator-1",
        suffix="stale",
        worktree=tmp_path / "elsewhere",
    )
    tree = build_tree([stale])
    attach_resumable_flags(tree, {stale.name: stale}, str(tmp_path))

    pruned = prune_to_resumable(tree)

    assert "orchestrator-1" not in pruned


def test_prune_to_resumable_keeps_a_non_resumable_ancestor_of_a_resumable_descendant(
    tmp_path: Path,
) -> None:
    ancestor = make_worker(
        tmp_path,
        parent_session_id="orchestrator-1",
        suffix="ancestor",
        worktree=tmp_path / "elsewhere",
    )
    descendant = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="descendant",
        parent_worker_name=ancestor.name,
    )
    tree = build_tree([ancestor, descendant])
    attach_resumable_flags(
        tree, {ancestor.name: ancestor, descendant.name: descendant}, str(tmp_path)
    )

    pruned = prune_to_resumable(tree)

    (ancestor_node,) = pruned["orchestrator-1"]
    assert ancestor_node["name"] == ancestor.name
    assert ancestor_node["resumable"] is False
    (descendant_node,) = ancestor_node["children"]
    assert descendant_node["name"] == descendant.name
    assert descendant_node["resumable"] is True


def test_prune_to_resumable_drops_a_cycle_marker_string_without_recursing(
    tmp_path: Path,
) -> None:
    worker_a = make_worker(
        tmp_path, parent_session_id=None, suffix="a", worktree=tmp_path / "elsewhere"
    )
    worker_b = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="b",
        parent_worker_name=worker_a.name,
        worktree=tmp_path / "elsewhere",
    )
    worker_a.parent_worker_name = worker_b.name
    tree = build_tree([worker_a, worker_b])
    attach_resumable_flags(
        tree, {worker_a.name: worker_a, worker_b.name: worker_b}, str(tmp_path)
    )

    pruned = prune_to_resumable(tree)

    key = "(cycle: unreachable from any root)"
    assert key not in pruned


def test_resolve_group_session_log_paths_calls_resolve_with_the_real_group_key(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    tree = build_tree([worker])
    calls: list[str] = []

    def fake_resolve(parent_session_id: str) -> str | None:
        calls.append(parent_session_id)
        return f"/logs/{parent_session_id}.jsonl"

    result = resolve_group_session_log_paths(tree, fake_resolve)

    assert result == {"orchestrator-1": "/logs/orchestrator-1.jsonl"}
    assert calls == ["orchestrator-1"]


def test_resolve_group_session_log_paths_never_calls_resolve_for_sentinel_keys(
    tmp_path: Path,
) -> None:
    unattributed = make_worker(tmp_path, parent_session_id=None, suffix="a")
    worker_with_missing_parent = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="b",
        parent_worker_name="ccw-does-not-exist",
    )
    tree = build_tree([unattributed, worker_with_missing_parent])
    calls: list[str] = []

    def fake_resolve(parent_session_id: str) -> str | None:
        calls.append(parent_session_id)
        return "/should-not-be-called"

    result = resolve_group_session_log_paths(tree, fake_resolve)

    assert result == {
        "(unknown parent)": None,
        "(orphaned: parent 'ccw-does-not-exist' not found)": None,
    }
    assert calls == []


def test_resolve_group_session_log_paths_sets_none_when_resolve_finds_nothing(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    tree = build_tree([worker])

    result = resolve_group_session_log_paths(tree, lambda parent_session_id: None)

    assert result == {"orchestrator-1": None}


def test_resolve_group_session_log_paths_does_not_mutate_the_tree(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    tree = build_tree([worker])
    before = {key: list(nodes) for key, nodes in tree.items()}

    resolve_group_session_log_paths(tree, lambda parent_session_id: "/logs/x.jsonl")

    assert tree == before

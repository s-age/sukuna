import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict, Unpack

from _terminal_fakes import make_worker as _make_worker

from sukuna.domain.entity.worker_record import WorkerRecord
from sukuna.domain.service.worker_tree import build_tree
from sukuna.presentation.tree_presenter import (
    format_local_timestamp,
    parse_local_datetime,
    render_compact_tree,
)


class _WorkerOverrides(TypedDict, total=False):
    pane_ref: str | None
    suffix: str | None
    goal: str | None
    parent_worker_name: str | None


def make_worker(
    repo: Path, *, parent_session_id: str | None, **overrides: Unpack[_WorkerOverrides]
) -> WorkerRecord:
    return _make_worker(repo, parent_session_id=parent_session_id, **overrides)


def test_render_compact_tree_draws_siblings_and_a_nested_child(tmp_path: Path) -> None:
    root_a = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    root_b = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="b")
    child = make_worker(
        tmp_path, parent_session_id=None, suffix="c", parent_worker_name=root_a.name
    )

    tree = build_tree([root_a, root_b, child])
    lines = render_compact_tree(tree).splitlines()

    assert lines == [
        "orchestrator-1",
        f"├── {root_a.name} ({format_local_timestamp(root_a.updated_at)})",
        f"│   └── {child.name} ({format_local_timestamp(child.updated_at)})",
        f"└── {root_b.name} ({format_local_timestamp(root_b.updated_at)})",
    ]


def test_render_compact_tree_omits_goal_and_state(tmp_path: Path) -> None:
    worker = make_worker(
        tmp_path, parent_session_id="orchestrator-1", suffix="a", goal="find the leak"
    )

    rendered = render_compact_tree(build_tree([worker]))

    assert "find the leak" not in rendered
    assert "ready" not in rendered


def test_render_compact_tree_prints_a_cycle_placeholder_as_is(tmp_path: Path) -> None:
    worker_b = make_worker(tmp_path, parent_session_id=None, suffix="b")
    worker_a = make_worker(
        tmp_path, parent_session_id=None, suffix="a", parent_worker_name=worker_b.name
    )
    worker_b.parent_worker_name = worker_a.name

    tree = build_tree([worker_a, worker_b])
    rendered = render_compact_tree(tree)

    assert "cycle detected" in rendered


def test_render_compact_tree_separates_multiple_groups_with_a_blank_line(
    tmp_path: Path,
) -> None:
    child_a = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    child_b = make_worker(tmp_path, parent_session_id="orchestrator-2", suffix="b")

    tree = build_tree([child_a, child_b])
    lines = render_compact_tree(tree).splitlines()

    assert lines == [
        "orchestrator-1",
        f"└── {child_a.name} ({format_local_timestamp(child_a.updated_at)})",
        "",
        "orchestrator-2",
        f"└── {child_b.name} ({format_local_timestamp(child_b.updated_at)})",
    ]


def test_render_compact_tree_appends_a_single_trailing_marker_for_hidden_parent_groups(
    tmp_path: Path,
) -> None:
    child_a = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")

    tree = build_tree([child_a])
    lines = render_compact_tree(tree, hidden_parent_groups=2).splitlines()

    assert lines == [
        "orchestrator-1",
        f"└── {child_a.name} ({format_local_timestamp(child_a.updated_at)})",
        "",
        "(+2 more parent groups)",
    ]


def test_render_compact_tree_tags_a_resumable_node(tmp_path: Path) -> None:
    resumable = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    stale = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="b")

    tree = build_tree([resumable, stale])
    tree["orchestrator-1"][0]["resumable"] = True
    tree["orchestrator-1"][1]["resumable"] = False
    rendered = render_compact_tree(tree)

    resumable_line = next(
        line for line in rendered.splitlines() if resumable.name in line
    )
    stale_line = next(line for line in rendered.splitlines() if stale.name in line)
    assert "[resumable]" in resumable_line
    assert "[resumable]" not in stale_line


def test_render_compact_tree_treats_a_missing_resumable_key_as_not_resumable(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")

    rendered = render_compact_tree(build_tree([worker]))

    assert "[resumable]" not in rendered


def test_render_compact_tree_omits_the_marker_when_no_parent_groups_are_hidden(
    tmp_path: Path,
) -> None:
    child_a = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")

    rendered = render_compact_tree(build_tree([child_a]), hidden_parent_groups=0)

    assert "more parent groups" not in rendered


def test_parse_local_datetime_is_the_inverse_of_format_local_timestamp() -> None:
    updated_at = "2026-08-22T00:03:37.332702+00:00"

    formatted = format_local_timestamp(updated_at)
    parsed = parse_local_datetime(formatted)

    assert parsed.astimezone(UTC).replace(microsecond=0) == datetime.fromisoformat(
        updated_at
    ).replace(microsecond=0)


def test_format_local_timestamp_matches_the_requested_shape() -> None:
    formatted = format_local_timestamp("2026-08-22T00:03:37.332702+00:00")

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", formatted)


def test_format_local_timestamp_preserves_the_instant_when_converting_to_local_time() -> (
    None
):
    base = datetime(2026, 8, 22, 0, 3, 37, tzinfo=UTC)
    later = base + timedelta(hours=2)

    formatted_base = format_local_timestamp(base.isoformat())
    formatted_later = format_local_timestamp(later.isoformat())

    delta = datetime.fromisoformat(formatted_later) - datetime.fromisoformat(
        formatted_base
    )
    assert delta == timedelta(hours=2)

import json
import sys
from pathlib import Path
from typing import TypedDict, Unpack

import pytest
from _terminal_fakes import make_worker as _make_worker

import sukuna.cli_human as cli_human_module
import sukuna.infrastructure.node_runtime as node_runtime_module
import sukuna.infrastructure.settings as settings_module
import sukuna.infrastructure.tui.launcher as tui_launcher_module
from sukuna.cli_human import main_cli as main
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.infrastructure.registry import Registry
from sukuna.presentation.tree_presenter import format_local_timestamp


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


def run_tree(registry_path: Path, capsys) -> str:
    exit_code = main(["--registry", str(registry_path), "tree"])
    assert exit_code == 0
    return capsys.readouterr().out


def test_tree_groups_workers_by_their_spawning_session(tmp_path: Path, capsys) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    child_a = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    child_b = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="b")
    other_child = make_worker(tmp_path, parent_session_id="orchestrator-2", suffix="c")
    unattributed = make_worker(tmp_path, parent_session_id=None, suffix="d")
    for worker in (child_a, child_b, other_child, unattributed):
        registry.add(worker)

    exit_code = main(["--registry", str(registry_path), "tree"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "orchestrator-1" in out
    assert "orchestrator-2" in out
    assert "(unknown parent)" in out
    assert f"{child_a.name} ({format_local_timestamp(child_a.updated_at)})" in out
    assert f"{child_b.name} ({format_local_timestamp(child_b.updated_at)})" in out
    assert (
        f"{other_child.name} ({format_local_timestamp(other_child.updated_at)})" in out
    )
    assert (
        f"{unattributed.name} ({format_local_timestamp(unattributed.updated_at)})"
        in out
    )


def test_tree_shows_updated_at_per_worker_line(tmp_path: Path, capsys) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(
        tmp_path, parent_session_id="orchestrator-1", pane_ref="%42", suffix="a"
    )
    registry.add(worker)
    updated = worker
    for state in (
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
        WorkerState.CLOSED,
    ):
        updated = registry.transition(worker.name, state)

    exit_code = main(["--registry", str(registry_path), "tree"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"{worker.name} ({format_local_timestamp(updated.updated_at)})" in out


def test_tree_renders_japanese_goal_as_literal_text(tmp_path: Path, capsys) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(
        tmp_path, parent_session_id="orchestrator-1", suffix="a", goal="日本語の目標"
    )
    registry.add(worker)

    exit_code = main(["--registry", str(registry_path), "tree"])

    assert exit_code == 0
    out = capsys.readouterr().out
    # tree's compact view omits goal entirely (see `inspect` for detail) --
    # this only guards against a `\u` escape ever leaking back into the name line.
    assert "\\u" not in out
    assert f"{worker.name} ({format_local_timestamp(worker.updated_at)})" in out


def test_tree_nests_grandworkers_via_parent_worker_name_with_box_drawing(
    tmp_path: Path, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker_a = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    worker_b = make_worker(
        tmp_path, parent_session_id=None, suffix="b", parent_worker_name=worker_a.name
    )
    worker_c = make_worker(
        tmp_path, parent_session_id=None, suffix="c", parent_worker_name=worker_b.name
    )
    for worker in (worker_a, worker_b, worker_c):
        registry.add(worker)

    exit_code = main(["--registry", str(registry_path), "tree"])

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "orchestrator-1"
    assert (
        lines[1]
        == f"└── {worker_a.name} ({format_local_timestamp(worker_a.updated_at)})"
    )
    assert (
        lines[2]
        == f"    └── {worker_b.name} ({format_local_timestamp(worker_b.updated_at)})"
    )
    assert (
        lines[3]
        == f"        └── {worker_c.name} ({format_local_timestamp(worker_c.updated_at)})"
    )


def test_tree_shows_orphaned_workers_whose_parent_is_missing(
    tmp_path: Path, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="a",
        parent_worker_name="ccw-ghost-parent-1",
    )
    registry.add(worker)

    exit_code = main(["--registry", str(registry_path), "tree"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "(orphaned: parent 'ccw-ghost-parent-1' not found)" in out
    assert f"{worker.name} ({format_local_timestamp(worker.updated_at)})" in out


def test_tree_surfaces_an_isolated_parent_cycle_unreachable_from_any_root(
    tmp_path: Path, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker_b = make_worker(tmp_path, parent_session_id=None, suffix="b")
    worker_a = make_worker(
        tmp_path, parent_session_id=None, suffix="a", parent_worker_name=worker_b.name
    )
    worker_b.parent_worker_name = worker_a.name
    registry.add(worker_a)
    registry.add(worker_b)

    exit_code = main(["--registry", str(registry_path), "tree"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "(cycle: unreachable from any root)" in out
    assert worker_a.name in out
    assert worker_b.name in out
    assert "cycle detected" in out


def test_tree_keeps_the_existing_grandworker_chain_intact_alongside_an_unrelated_cycle(
    tmp_path: Path, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    root = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="root")
    child = make_worker(
        tmp_path, parent_session_id=None, suffix="child", parent_worker_name=root.name
    )
    grandchild = make_worker(
        tmp_path, parent_session_id=None, suffix="grand", parent_worker_name=child.name
    )
    cycle_a = make_worker(tmp_path, parent_session_id=None, suffix="cyc-a")
    cycle_b = make_worker(
        tmp_path,
        parent_session_id=None,
        suffix="cyc-b",
        parent_worker_name=cycle_a.name,
    )
    cycle_a.parent_worker_name = cycle_b.name
    for worker in (root, child, grandchild, cycle_a, cycle_b):
        registry.add(worker)

    out = run_tree(registry_path, capsys)

    assert f"{root.name} ({format_local_timestamp(root.updated_at)})" in out
    assert f"{child.name} ({format_local_timestamp(child.updated_at)})" in out
    assert f"{grandchild.name} ({format_local_timestamp(grandchild.updated_at)})" in out
    assert "(cycle: unreachable from any root)" in out
    assert cycle_a.name in out
    assert cycle_b.name in out


def test_tree_includes_a_worker_that_never_got_a_pane(tmp_path: Path, capsys) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    registry.add(worker)
    updated = registry.transition(worker.name, WorkerState.FAILED)

    exit_code = main(["--registry", str(registry_path), "tree"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"{worker.name} ({format_local_timestamp(updated.updated_at)})" in out


def test_tree_defaults_to_the_5_most_recent_parent_groups(
    tmp_path: Path, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    workers = [
        make_worker(tmp_path, parent_session_id=f"orchestrator-{i}", suffix=f"w{i}")
        for i in range(6)
    ]
    for index, worker in enumerate(workers):
        worker.updated_at = f"2026-08-1{index}T00:00:00+00:00"
        registry.add(worker)

    exit_code = main(["--registry", str(registry_path), "tree"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert workers[0].name not in out
    for worker in workers[1:]:
        assert worker.name in out
    assert "(+1 more parent groups)" in out


def test_tree_limit_flag_overrides_the_default(tmp_path: Path, capsys) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    workers = [
        make_worker(tmp_path, parent_session_id=f"orchestrator-{i}", suffix=f"w{i}")
        for i in range(3)
    ]
    for index, worker in enumerate(workers):
        worker.updated_at = f"2026-08-1{index}T00:00:00+00:00"
        registry.add(worker)

    exit_code = main(["--registry", str(registry_path), "tree", "--limit", "1"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert workers[2].name in out
    assert workers[1].name not in out
    assert workers[0].name not in out
    assert "(+2 more parent groups)" in out


def test_tree_since_and_to_flags_filter_by_a_parent_groups_latest_update(
    tmp_path: Path, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    old = make_worker(tmp_path, parent_session_id="orchestrator-old", suffix="old")
    old.updated_at = "2020-01-01T00:00:00+00:00"
    recent = make_worker(tmp_path, parent_session_id="orchestrator-new", suffix="new")
    recent.updated_at = "2030-01-01T00:00:00+00:00"
    registry.add(old)
    registry.add(recent)

    exit_code = main(
        ["--registry", str(registry_path), "tree", "--since", "2025-01-01"]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert old.name not in out
    assert recent.name in out


def test_tree_rejects_an_invalid_since_value(tmp_path: Path, capsys) -> None:
    registry_path = tmp_path / "registry.json"
    Registry(registry_path)

    exit_code = main(
        ["--registry", str(registry_path), "tree", "--since", "not-a-datetime"]
    )

    assert exit_code == 2


def test_tree_treats_special_buckets_as_parent_groups_alongside_session_groups(
    tmp_path: Path, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    session_worker = make_worker(
        tmp_path, parent_session_id="orchestrator-1", suffix="s"
    )
    session_worker.updated_at = "2026-08-01T00:00:00+00:00"
    registry.add(session_worker)
    orphan_worker = make_worker(
        tmp_path, parent_session_id=None, suffix="o", parent_worker_name="ghost"
    )
    orphan_worker.updated_at = "2026-08-20T00:00:00+00:00"
    registry.add(orphan_worker)

    exit_code = main(["--registry", str(registry_path), "tree", "--limit", "1"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert orphan_worker.name in out
    assert session_worker.name not in out
    assert out.count("(+1 more parent groups)") == 1


def test_tree_never_truncates_a_shown_groups_members(tmp_path: Path, capsys) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    root = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="root")
    registry.add(root)
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
        registry.add(child)

    exit_code = main(["--registry", str(registry_path), "tree", "--limit", "1"])

    assert exit_code == 0
    out = capsys.readouterr().out
    for child in children:
        assert child.name in out
    assert "more parent groups" not in out


def test_tree_orders_parent_groups_by_most_recent_update_descending(
    tmp_path: Path, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    workers = [
        make_worker(tmp_path, parent_session_id=f"orchestrator-{i}", suffix=f"w{i}")
        for i in range(3)
    ]
    for index, worker in enumerate(workers):
        worker.updated_at = f"2026-08-2{index}T00:00:00+00:00"
        registry.add(worker)

    exit_code = main(["--registry", str(registry_path), "tree"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert (
        out.index(workers[2].name)
        < out.index(workers[1].name)
        < out.index(workers[0].name)
    )


def test_tree_tui_requires_stdin_to_be_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    Registry(registry_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    exit_code = main(["--registry", str(registry_path), "tree", "--tui"])

    assert exit_code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"] == "VALIDATION_ERROR"
    assert "stdin and stdout must both be a TTY" in output["message"]


def test_tree_tui_requires_stdout_to_be_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    Registry(registry_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    exit_code = main(["--registry", str(registry_path), "tree", "--tui"])

    assert exit_code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"] == "VALIDATION_ERROR"
    assert "stdin and stdout must both be a TTY" in output["message"]


def test_tree_tui_dispatches_to_the_tui_use_case_when_both_streams_are_ttys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    Registry(registry_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    captured: dict[str, object] = {}

    def fake_launch(registry, *, since, to, cwd, resumable_only) -> int:
        captured["since"] = since
        captured["to"] = to
        captured["cwd"] = cwd
        captured["resumable_only"] = resumable_only
        return 5

    # `cli_human.py` imports this via `from .usecase.tree import launch_tree_tui
    # as launch_tree_tui_use_case` -- a from-import binding, so the patch
    # target is the name inside `cli_human_module`, not `usecase.tree` itself.
    # `launch_tree_tui_use_case` takes no `limit` argument at all (the TUI
    # always shows every group) -- a stub that still accepted `limit`
    # would silently hide a regression that started passing `--limit`'s
    # value back through here.
    monkeypatch.setattr(cli_human_module, "launch_tree_tui_use_case", fake_launch)

    exit_code = main(["--registry", str(registry_path), "tree", "--tui"])

    assert exit_code == 5
    assert captured == {
        "since": None,
        "to": None,
        "cwd": str(Path.cwd().resolve()),
        "resumable_only": False,
    }


def _stub_all_auto_tui_gates_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(settings_module, "load_tui_enabled", lambda: True)
    monkeypatch.setattr(node_runtime_module, "node_satisfies_tui_minimum", lambda: True)
    monkeypatch.setattr(tui_launcher_module, "bundle_present", lambda: True)


def test_tree_with_no_flags_auto_launches_the_tui_when_every_gate_is_satisfied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    Registry(registry_path)
    _stub_all_auto_tui_gates_open(monkeypatch)
    captured: dict[str, object] = {}

    def fake_launch(registry, *, since, to, cwd, resumable_only) -> int:
        captured["since"] = since
        captured["to"] = to
        captured["cwd"] = cwd
        captured["resumable_only"] = resumable_only
        return 9

    monkeypatch.setattr(cli_human_module, "launch_tree_tui_use_case", fake_launch)

    exit_code = main(["--registry", str(registry_path), "tree", "--limit", "1"])

    assert exit_code == 9
    assert captured == {
        "since": None,
        "to": None,
        "cwd": str(Path.cwd().resolve()),
        "resumable_only": False,
    }


def test_tree_with_no_flags_falls_back_to_text_when_tui_enabled_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    registry.add(worker)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(settings_module, "load_tui_enabled", lambda: False)
    monkeypatch.setattr(node_runtime_module, "node_satisfies_tui_minimum", lambda: True)
    monkeypatch.setattr(tui_launcher_module, "bundle_present", lambda: True)

    exit_code = main(["--registry", str(registry_path), "tree"])

    assert exit_code == 0
    assert worker.name in capsys.readouterr().out


def test_tree_text_flag_forces_text_output_even_when_every_tui_gate_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    registry.add(worker)
    _stub_all_auto_tui_gates_open(monkeypatch)

    def fail_if_called(*args: object, **kwargs: object) -> int:
        raise AssertionError("--text must never dispatch to the TUI use case")

    monkeypatch.setattr(cli_human_module, "launch_tree_tui_use_case", fail_if_called)

    exit_code = main(["--registry", str(registry_path), "tree", "--text"])

    assert exit_code == 0
    assert worker.name in capsys.readouterr().out


def test_tree_rejects_tui_and_text_together(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    Registry(registry_path)

    with pytest.raises(SystemExit) as excinfo:
        main(["--registry", str(registry_path), "tree", "--tui", "--text"])

    assert excinfo.value.code == 2


def _make_resumable_and_stale_workers(
    tmp_path: Path, *, resumable_parent: str, stale_parent: str
) -> tuple[WorkerRecord, WorkerRecord, Path]:
    here = (tmp_path / "here").resolve()
    here.mkdir()
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    resumable = make_worker(
        here, parent_session_id=resumable_parent, suffix="resumable", worktree=here
    )
    stale = make_worker(
        here, parent_session_id=stale_parent, suffix="stale", worktree=elsewhere
    )
    return resumable, stale, here


def test_tree_tags_a_worker_resumable_from_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    resumable, stale, here = _make_resumable_and_stale_workers(
        tmp_path, resumable_parent="orchestrator-1", stale_parent="orchestrator-1"
    )
    registry = Registry(tmp_path / "registry.json")
    registry.add(resumable)
    registry.add(stale)
    monkeypatch.chdir(here)

    exit_code = main(["--registry", str(tmp_path / "registry.json"), "tree"])

    assert exit_code == 0
    out = capsys.readouterr().out
    resumable_line = next(line for line in out.splitlines() if resumable.name in line)
    stale_line = next(line for line in out.splitlines() if stale.name in line)
    assert "[resumable]" in resumable_line
    assert "[resumable]" not in stale_line


def test_tree_resumable_only_prunes_a_group_with_no_resumable_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    resumable, stale, here = _make_resumable_and_stale_workers(
        tmp_path,
        resumable_parent="orchestrator-resumable",
        stale_parent="orchestrator-stale",
    )
    registry = Registry(tmp_path / "registry.json")
    registry.add(resumable)
    registry.add(stale)
    monkeypatch.chdir(here)

    exit_code = main(
        ["--registry", str(tmp_path / "registry.json"), "tree", "--resumable-only"]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert resumable.name in out
    assert stale.name not in out
    assert "orchestrator-stale" not in out


def test_tree_tui_passes_cwd_and_resumable_only_through_to_the_tui_use_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    Registry(registry_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    captured: dict[str, object] = {}

    def fake_launch(registry, *, since, to, cwd, resumable_only) -> int:
        captured["cwd"] = cwd
        captured["resumable_only"] = resumable_only
        return 0

    monkeypatch.setattr(cli_human_module, "launch_tree_tui_use_case", fake_launch)

    exit_code = main(
        ["--registry", str(registry_path), "tree", "--tui", "--resumable-only"]
    )

    assert exit_code == 0
    assert captured == {"cwd": str(Path.cwd().resolve()), "resumable_only": True}

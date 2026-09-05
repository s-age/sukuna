"""Unit tests for `scripts/backfill_session_log_paths.py` -- a standalone
script outside the `sukuna` package, loaded here via `importlib` since it
isn't importable as a regular module."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.domain.service.session_log import encode_project_dir_name
from sukuna.infrastructure.registry import Registry

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "backfill_session_log_paths.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "backfill_session_log_paths", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backfill_module = _load_script()


def _worker(registry: Registry, name: str, worktree: Path) -> None:
    record = WorkerRecord.create(
        name=name, repo_root=str(worktree), worktree=str(worktree)
    )
    registry.add(record)
    registry.mutate(name, lambda w: w.transition_to(WorkerState.READY))


def _project_dir(projects_dir: Path, worktree: Path) -> Path:
    project_dir = projects_dir / encode_project_dir_name(str(worktree))
    project_dir.mkdir(parents=True)
    return project_dir


def test_build_index_picks_the_candidate_with_the_largest_mtime(
    tmp_path: Path,
) -> None:
    """§4.5's winner-selection rule: two jsonls in the same directory match
    the same worker name (a respawn leftover) -- the index must keep the
    one with the larger mtime, not whichever the scan happens to see
    last."""
    projects_dir = tmp_path / "projects"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    custom_title = '{"type":"custom-title","customTitle":"ccw-x-1","sessionId":"s"}\n'
    older = project_dir / "z-older.jsonl"  # sorts after `newer` lexically
    older.write_text(custom_title)
    newer = project_dir / "a-newer.jsonl"
    newer.write_text(custom_title)
    now = 2_000_000_000.0
    os.utime(older, (now - 3600, now - 3600))
    os.utime(newer, (now, now))

    index = backfill_module.build_index(projects_dir, frozenset({"ccw-x-1"}))

    assert index[("-Users-example-repo", "ccw-x-1")] == (str(newer), now)


def test_build_index_ignores_names_outside_the_requested_set(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    (project_dir / "session.jsonl").write_text(
        '{"type":"custom-title","customTitle":"ccw-x-not-requested","sessionId":"s"}\n'
    )

    index = backfill_module.build_index(projects_dir, frozenset({"ccw-x-1"}))

    assert index == {}


def test_build_index_indexes_one_jsonl_under_every_name_it_matches(
    tmp_path: Path,
) -> None:
    """A session renamed mid-run can leave more than one `custom-title`
    entry among a jsonl's leading lines -- matching `resolve_session_log_path()`'s
    own per-name semantics means one file can legitimately be the answer
    for more than one requested name, not just whichever matches first."""
    projects_dir = tmp_path / "projects"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    renamed = project_dir / "renamed-session.jsonl"
    renamed.write_text(
        '{"type":"custom-title","customTitle":"ccw-x-old-name","sessionId":"s"}\n'
        '{"type":"custom-title","customTitle":"ccw-x-new-name","sessionId":"s"}\n'
    )

    index = backfill_module.build_index(
        projects_dir, frozenset({"ccw-x-old-name", "ccw-x-new-name"})
    )

    assert index[("-Users-example-repo", "ccw-x-old-name")][0] == str(renamed)
    assert index[("-Users-example-repo", "ccw-x-new-name")][0] == str(renamed)


def test_index_resolver_returns_none_when_not_indexed(tmp_path: Path) -> None:
    resolver = backfill_module.make_index_resolver({})

    assert resolver(str(tmp_path), "ccw-x-1", None) is None


def test_index_resolver_rejects_a_candidate_older_than_not_before(
    tmp_path: Path,
) -> None:
    worktree = str(tmp_path)
    dir_name = encode_project_dir_name(worktree)
    index = {(dir_name, "ccw-x-1"): ("/logs/stale.jsonl", 2_000_000_000.0)}
    resolver = backfill_module.make_index_resolver(index)

    assert resolver(worktree, "ccw-x-1", "2033-05-18T04:33:20+00:00") is None


def test_index_resolver_accepts_a_candidate_no_older_than_not_before(
    tmp_path: Path,
) -> None:
    worktree = str(tmp_path)
    dir_name = encode_project_dir_name(worktree)
    index = {(dir_name, "ccw-x-1"): ("/logs/fresh.jsonl", 2_000_000_000.0)}
    resolver = backfill_module.make_index_resolver(index)

    assert (
        resolver(worktree, "ccw-x-1", "2033-05-18T03:33:20+00:00")
        == "/logs/fresh.jsonl"
    )


def test_backfill_resolves_unresolved_workers_and_leaves_resolved_ones_alone(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _worker(registry, "ccw-x-unresolved", worktree)
    already_resolved = "ccw-x-already-resolved"
    _worker(registry, already_resolved, worktree)
    registry.mutate(
        already_resolved, lambda w: setattr(w, "session_log_path", "/logs/kept.jsonl")
    )
    projects_dir = tmp_path / "projects"
    project_dir = _project_dir(projects_dir, worktree)
    (project_dir / "session.jsonl").write_text(
        '{"type":"custom-title","customTitle":"ccw-x-unresolved","sessionId":"s"}\n'
    )

    updated_count = backfill_module.backfill(registry, projects_dir)

    assert updated_count == 1
    assert registry.get("ccw-x-unresolved").session_log_path == str(
        project_dir / "session.jsonl"
    )
    assert registry.get(already_resolved).session_log_path == "/logs/kept.jsonl"


def test_backfill_skips_a_starting_worker(tmp_path: Path) -> None:
    """A worker mid-respawn (STARTING) must be left for its own READY
    transition to resolve, not written by the backfill (card §4.5's race
    note)."""
    registry = Registry(tmp_path / "registry.json")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    name = "ccw-x-starting"
    record = WorkerRecord.create(
        name=name, repo_root=str(worktree), worktree=str(worktree)
    )
    registry.add(record)  # WorkerRecord.create() always starts as STARTING
    projects_dir = tmp_path / "projects"
    project_dir = _project_dir(projects_dir, worktree)
    (project_dir / "session.jsonl").write_text(
        '{"type":"custom-title","customTitle":"ccw-x-starting","sessionId":"s"}\n'
    )

    updated_count = backfill_module.backfill(registry, projects_dir)

    assert updated_count == 0
    assert registry.get(name).session_log_path is None


def test_backfill_is_idempotent_on_a_second_run(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _worker(registry, "ccw-x-1", worktree)
    projects_dir = tmp_path / "projects"
    project_dir = _project_dir(projects_dir, worktree)
    (project_dir / "session.jsonl").write_text(
        '{"type":"custom-title","customTitle":"ccw-x-1","sessionId":"s"}\n'
    )

    first_run = backfill_module.backfill(registry, projects_dir)
    second_run = backfill_module.backfill(registry, projects_dir)

    assert first_run == 1
    assert second_run == 0

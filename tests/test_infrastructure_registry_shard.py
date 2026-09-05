"""Shard I/O and migration crash-safety:
`infrastructure/registry_shard.py`'s shard discovery/resolution helpers,
`MergedRegistryView`'s read-only fan-out, and `migrate_if_needed()`'s
staging + atomic-rename + `.bak` sequence, including the empty-`registry/`
hazard, concurrent-migration fallthrough, and stale-staging-dir cleanup."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _terminal_fakes import make_worker as _make_worker

import sukuna.infrastructure.claude_projects as claude_projects_module
import sukuna.infrastructure.registry_shard as registry_shard_module
from sukuna.domain.entity.worker_record import WorkerRecord
from sukuna.domain.mapper.registry_mapper import record_to_dict
from sukuna.domain.service.session_log import encode_project_dir_name
from sukuna.errors import NotFoundError, RegistryIOError, ValidationError
from sukuna.infrastructure.registry import Registry

_REPO = Path("/repo")


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


def _legacy_path() -> Path:
    return registry_shard_module.legacy_registry_path()


def _write_legacy_registry(workers: list[WorkerRecord]) -> None:
    legacy = _legacy_path()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 3,
        "workers": [record_to_dict(worker) for worker in workers],
        "ordinal_high_water_marks": {},
        "last_swept_at": None,
    }
    legacy.write_text(json.dumps(payload), encoding="utf-8")


# --- shard discovery / resolution -------------------------------------


def test_list_shard_paths_is_empty_when_no_shard_dir_exists() -> None:
    assert registry_shard_module.list_shard_paths() == []


def test_list_shard_paths_is_sorted() -> None:
    directory = registry_shard_module.shard_dir()
    directory.mkdir(parents=True)
    (directory / "zzz.json").write_text("{}", encoding="utf-8")
    (directory / "aaa.json").write_text("{}", encoding="utf-8")

    paths = registry_shard_module.list_shard_paths()

    assert [p.name for p in paths] == ["aaa.json", "zzz.json"]


def test_registry_for_root_spawn_creates_a_cwd_keyed_shard_when_none_exists() -> None:
    registry = registry_shard_module.registry_for_root_spawn(
        parent_session_id="s1", cwd="/some/cwd"
    )

    assert registry.path == registry_shard_module.shard_path_for_key(
        encode_project_dir_name("/some/cwd")
    )


def test_registry_for_root_spawn_reuses_an_existing_shard_for_the_same_session() -> (
    None
):
    first = registry_shard_module.registry_for_root_spawn(
        parent_session_id="s1", cwd="/cwd-one"
    )
    first.add(_make_worker(_REPO, name="ccw-root-1", parent_session_id="s1"))

    second = registry_shard_module.registry_for_root_spawn(
        parent_session_id="s1", cwd="/a-totally-different-cwd"
    )

    assert second.path == first.path


def test_registry_for_child_spawn_rejects_cross_shard_grafting() -> None:
    """Cross-shard grafting guard: a caller whose own session *already has
    records in a shard* (`find_shard_for_parent_session()`) different
    from the named
    `parent_worker`'s actual shard must be rejected outright, not silently
    grafted onto the unrelated tree -- see
    `domain.service.shard_resolution.check_cross_shard_grafting()`. This is
    the exact scenario that let one session's records split across two
    shards and re-mint an already-used ordinal. (A record-less caller
    session is the standard grandchild flow and must NOT be rejected --
    see the "allows_nested_spawn" tests below.)"""
    root_registry = registry_shard_module.registry_for_root_spawn(
        parent_session_id="s1", cwd="/cwd"
    )
    root_registry.add(_make_worker(_REPO, name="ccw-parent", parent_session_id="s1"))
    # "s2" already has its own, unrelated record in a different shard.
    registry_shard_module.registry_for_root_spawn(
        parent_session_id="s2", cwd="/unrelated-cwd"
    ).add(_make_worker(_REPO, name="ccw-unrelated", parent_session_id="s2"))

    with pytest.raises(ValidationError):
        registry_shard_module.registry_for_child_spawn(
            parent_worker_name="ccw-parent", parent_session_id="s2", cwd="/other-cwd"
        )


def test_registry_for_child_spawn_allows_nested_spawn_when_caller_reuses_the_parents_session() -> (
    None
):
    """The guard must not affect the legitimate case: the same calling
    session that already owns the parent's shard (reuse-first finds it)
    naming an existing worker of its own as `parent_worker`."""
    root_registry = registry_shard_module.registry_for_root_spawn(
        parent_session_id="s1", cwd="/cwd"
    )
    root_registry.add(_make_worker(_REPO, name="ccw-parent", parent_session_id="s1"))

    child_registry = registry_shard_module.registry_for_child_spawn(
        parent_worker_name="ccw-parent", parent_session_id="s1", cwd="/other-cwd"
    )

    assert child_registry.path == root_registry.path


def test_registry_for_child_spawn_allows_nested_spawn_from_the_same_cwd() -> None:
    """The other legitimate case: a different (first-time) calling session
    whose own cwd-derived shard key happens to match the parent's shard --
    e.g. a worker spawning its own grandchild from the same worktree."""
    root_registry = registry_shard_module.registry_for_root_spawn(
        parent_session_id="s1", cwd="/cwd"
    )
    root_registry.add(_make_worker(_REPO, name="ccw-parent", parent_session_id="s1"))

    child_registry = registry_shard_module.registry_for_child_spawn(
        parent_worker_name="ccw-parent", parent_session_id="s2", cwd="/cwd"
    )

    assert child_registry.path == root_registry.path


def test_registry_for_child_spawn_falls_back_to_root_rule_for_an_unknown_parent() -> (
    None
):
    registry = registry_shard_module.registry_for_child_spawn(
        parent_worker_name="ccw-nonexistent", parent_session_id="s1", cwd="/cwd"
    )

    assert registry.path == registry_shard_module.shard_path_for_key(
        encode_project_dir_name("/cwd")
    )


def test_registry_for_name_finds_the_right_shard() -> None:
    shard_a = Registry(registry_shard_module.shard_path_for_key("a"))
    shard_a.add(_make_worker(_REPO, name="ccw-in-a"))
    shard_b = Registry(registry_shard_module.shard_path_for_key("b"))
    shard_b.add(_make_worker(_REPO, name="ccw-in-b"))

    assert registry_shard_module.registry_for_name("ccw-in-b").path == shard_b.path  # type: ignore[union-attr]


def test_registry_for_name_returns_none_when_not_found() -> None:
    assert registry_shard_module.registry_for_name("ccw-nowhere") is None


# --- MergedRegistryView -------------------------------------------------


def test_merged_registry_view_lists_and_gets_across_shards() -> None:
    shard_a = Registry(registry_shard_module.shard_path_for_key("a"))
    shard_a.add(_make_worker(_REPO, name="ccw-in-a"))
    shard_b = Registry(registry_shard_module.shard_path_for_key("b"))
    shard_b.add(_make_worker(_REPO, name="ccw-in-b"))

    view = registry_shard_module.MergedRegistryView([shard_a, shard_b])

    assert {w.name for w in view.list()} == {"ccw-in-a", "ccw-in-b"}
    assert view.get("ccw-in-b").name == "ccw-in-b"
    with pytest.raises(NotFoundError):
        view.get("ccw-nowhere")


def test_merged_registry_view_rejects_every_mutation() -> None:
    view = registry_shard_module.MergedRegistryView([])

    with pytest.raises(ValidationError):
        view.add(_make_worker(_REPO, name="ccw-x"))
    with pytest.raises(ValidationError):
        view.next_ordinal("s1")
    with pytest.raises(ValidationError):
        view.purge(retention_days=30, now=datetime.now(UTC))


# --- migration -----------------------------------------------------------


def test_migrate_if_needed_is_a_no_op_on_a_fresh_install() -> None:
    registry_shard_module.migrate_if_needed()

    assert not registry_shard_module.shard_dir().exists()
    assert not _legacy_path().exists()


def test_migrate_if_needed_splits_records_by_root_worktree_when_no_transcript_exists() -> (
    None
):
    root = _make_worker(
        _REPO, name="ccw-root", parent_session_id="s1", worktree=Path("/proj-a")
    )
    child = _make_worker(
        _REPO,
        name="ccw-child",
        parent_session_id="child-session",
        parent_worker_name="ccw-root",
        worktree=Path("/proj-a/worker-worktree"),
    )
    _write_legacy_registry([root, child])

    registry_shard_module.migrate_if_needed()

    shard_path = registry_shard_module.shard_path_for_key(
        encode_project_dir_name("/proj-a")
    )
    assert shard_path.exists()
    names = {w.name for w in Registry(shard_path).list()}
    assert names == {"ccw-root", "ccw-child"}
    assert not _legacy_path().exists()
    assert registry_shard_module.legacy_backup_path().exists()


def test_migrate_if_needed_prefers_the_transcript_directory_over_the_root_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe's own headline finding (41% mismatch rate): when a
    transcript for the root's `parent_session_id` is found, Tier 1 wins
    even though the root's own `worktree` field points elsewhere."""
    root = _make_worker(
        _REPO,
        name="ccw-root",
        parent_session_id="s1",
        worktree=Path("/mismatched-worktree"),
    )
    _write_legacy_registry([root])
    projects_dir = registry_shard_module.sukuna_state_dir() / "fake-claude-projects"
    transcript_dir = projects_dir / "-Users-someone-real-project"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "s1.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        claude_projects_module, "default_projects_dir", lambda: projects_dir
    )

    registry_shard_module.migrate_if_needed()

    shard_path = registry_shard_module.shard_path_for_key("-Users-someone-real-project")
    assert shard_path.exists()
    assert {w.name for w in Registry(shard_path).list()} == {"ccw-root"}
    unexpected = registry_shard_module.shard_path_for_key(
        encode_project_dir_name("/mismatched-worktree")
    )
    assert not unexpected.exists()


def test_migrate_if_needed_keeps_one_session_together_across_two_root_worktrees() -> (
    None
):
    """End-to-end regression for the advisor-flagged Tier 2 gap: one
    session that ROOT-spawned into two different worktrees (no transcript
    for either) must migrate its records to exactly one shard, not two."""
    root_a = _make_worker(
        _REPO, name="ccw-root-a", parent_session_id="session-s", worktree=Path("/wt-a")
    )
    root_b = _make_worker(
        _REPO, name="ccw-root-b", parent_session_id="session-s", worktree=Path("/wt-b")
    )
    _write_legacy_registry([root_a, root_b])

    registry_shard_module.migrate_if_needed()

    shard_paths = registry_shard_module.list_shard_paths()
    assert len(shard_paths) == 1
    assert {w.name for w in Registry(shard_paths[0]).list()} == {
        "ccw-root-a",
        "ccw-root-b",
    }


def test_migrate_if_needed_routes_a_none_session_worker_to_the_catch_all_shard() -> (
    None
):
    manual = _make_worker(_REPO, name="ccw-manual", parent_session_id=None)
    _write_legacy_registry([manual])

    registry_shard_module.migrate_if_needed()

    catch_all = registry_shard_module.shard_path_for_key(
        registry_shard_module.CATCH_ALL_SHARD_KEY
    )
    assert catch_all.exists()
    assert {w.name for w in Registry(catch_all).list()} == {"ccw-manual"}


def test_migrate_if_needed_is_idempotent() -> None:
    _write_legacy_registry(
        [_make_worker(_REPO, name="ccw-root", parent_session_id="s1")]
    )

    registry_shard_module.migrate_if_needed()
    shards_before = {p.name for p in registry_shard_module.list_shard_paths()}
    registry_shard_module.migrate_if_needed()
    shards_after = {p.name for p in registry_shard_module.list_shard_paths()}

    assert shards_before == shards_after
    assert not _legacy_path().exists()


def test_migrate_if_needed_recovers_when_registry_dir_exists_but_is_empty() -> None:
    """Fix 1 (advisor-flagged hazard): an empty `registry/` -- e.g. created
    as a side effect of some unrelated shard read racing ahead of migration
    -- must not be mistaken for "already committed". The full migration
    must still run, not just a silent `.bak` rename that would lose every
    record."""
    _write_legacy_registry(
        [_make_worker(_REPO, name="ccw-root", parent_session_id="s1")]
    )
    registry_shard_module.shard_dir().mkdir(parents=True)

    registry_shard_module.migrate_if_needed()

    shard_paths = registry_shard_module.list_shard_paths()
    assert len(shard_paths) == 1
    assert {w.name for w in Registry(shard_paths[0]).list()} == {"ccw-root"}
    assert not _legacy_path().exists()


def test_migrate_if_needed_finishes_a_pending_bak_rename_without_restaging() -> None:
    """`registry/` already committed (non-empty) + legacy still present:
    only the `.bak` rename should run -- the shard content must be left
    exactly as it is, not recomputed."""
    shard_path = registry_shard_module.shard_path_for_key("already-committed")
    Registry(shard_path).add(_make_worker(_REPO, name="ccw-committed"))
    # A *different* legacy file is deliberately left behind, to prove its
    # contents are never read/re-staged once `registry/` is committed.
    _write_legacy_registry([_make_worker(_REPO, name="ccw-should-not-be-staged")])

    registry_shard_module.migrate_if_needed()

    assert {w.name for w in Registry(shard_path).list()} == {"ccw-committed"}
    assert not _legacy_path().exists()
    assert registry_shard_module.legacy_backup_path().exists()
    # No new shard was created from the stale legacy file's contents.
    assert len(registry_shard_module.list_shard_paths()) == 1


def test_migrate_if_needed_falls_through_when_a_concurrent_migrator_wins_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates the documented race: this process's own staging `rename()`
    fails because another process already committed `registry/` first.
    The fallthrough must adopt the existing commit and still finish the
    `.bak` rename, without raising."""
    _write_legacy_registry(
        [_make_worker(_REPO, name="ccw-mine", parent_session_id="s1")]
    )

    real_rename = Path.rename
    winner_shard_path = registry_shard_module.shard_path_for_key("winner")

    def _fake_rename(self: Path, target: str | os.PathLike[str]) -> Path:
        if self.name.startswith(".registry-migrating-"):
            # Another process "wins": populate the target directory with
            # its own committed shard before this rename can succeed.
            target_path = Path(target)
            target_path.mkdir(parents=True, exist_ok=True)
            Registry(winner_shard_path).add(_make_worker(_REPO, name="ccw-winner"))
            raise OSError(39, "Directory not empty")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", _fake_rename)

    registry_shard_module.migrate_if_needed()

    assert {w.name for w in Registry(winner_shard_path).list()} == {"ccw-winner"}
    assert not _legacy_path().exists()
    assert registry_shard_module.legacy_backup_path().exists()
    # This process's own staged data was discarded, not silently kept
    # alongside the winner's.
    remaining = {p.name for p in registry_shard_module.list_shard_paths()}
    assert remaining == {"winner.json"}


def test_migrate_if_needed_cleans_up_a_stale_staging_dir_from_a_dead_pid() -> None:
    dead_pid = 999999
    while registry_shard_module._pid_is_alive(dead_pid):  # pragma: no cover - defensive
        dead_pid += 1
    stale = registry_shard_module.sukuna_state_dir() / f".registry-migrating-{dead_pid}"
    stale.mkdir(parents=True)
    (stale / "junk.json").write_text("{}", encoding="utf-8")
    _write_legacy_registry(
        [_make_worker(_REPO, name="ccw-root", parent_session_id="s1")]
    )

    registry_shard_module.migrate_if_needed()

    assert not stale.exists()


def test_migrate_if_needed_never_touches_a_staging_dir_whose_pid_is_alive() -> None:
    live = (
        registry_shard_module.sukuna_state_dir() / f".registry-migrating-{os.getpid()}"
    )
    live.mkdir(parents=True)
    (live / "keep.json").write_text("{}", encoding="utf-8")

    registry_shard_module._cleanup_stale_staging_dirs()

    assert live.exists()


def test_migrate_if_needed_translates_a_raw_os_error_into_registry_io_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 2 self-review finding: a raw filesystem failure from this
    module's own `mkdir()`/`rename()` calls (outside any `Registry`'s own
    locked I/O, which already translates its own failures) must not leak
    past the project's `RegistryIOError` vocabulary."""
    _write_legacy_registry(
        [_make_worker(_REPO, name="ccw-root", parent_session_id="s1")]
    )

    def _failing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "mkdir", _failing_mkdir)

    with pytest.raises(RegistryIOError):
        registry_shard_module.migrate_if_needed()

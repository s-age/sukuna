"""CLI-level end-to-end tests for the sharded registry:
migration from a legacy single-file registry, ROOT/child shard resolution
across `sukuna spawn` invocations (including the same-session,
multiple-cwd case the acceptance criteria calls out by name), by-name
resolution transparently serving close/state/accept/respawn/capture,
tree/inspect/reconcile fanning out across every shard, and shard-level
lock non-interference -- all driven through `sukuna.cli.main()`/
`sukuna.cli_human.main_cli()` without the `--registry` override, so the
real sharding path is exercised end to end."""

from __future__ import annotations

import fcntl
import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from _terminal_fakes import (
    OtherBackend,
    RecordingBackend,
    install_backends,
    patch_pane_resolution,
)
from _terminal_fakes import make_worker as _make_worker

import sukuna.infrastructure.registry_shard as registry_shard_module
import sukuna.usecase.spawn as spawn_module
from sukuna.cli import main
from sukuna.cli_human import main_cli
from sukuna.domain.mapper.registry_mapper import record_to_dict
from sukuna.domain.service.session_log import encode_project_dir_name
from sukuna.infrastructure.registry import Registry


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    install_backends(monkeypatch, {"tmux": RecordingBackend, "iterm2": OtherBackend})
    patch_pane_resolution(monkeypatch, spawn_module)


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _spawn(
    specs: list[dict], monkeypatch: pytest.MonkeyPatch, capsys
) -> tuple[int, dict]:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(specs)))
    exit_code = main(["spawn"])
    payload = json.loads(capsys.readouterr().out)
    return exit_code, payload


def _spec(repo: Path, *, parent_worker: str | None = None) -> dict:
    spec: dict = {"role": "review", "repo": str(repo), "worktree": str(repo)}
    if parent_worker is not None:
        spec["parent_worker"] = parent_worker
    return spec


def _spawn_and_get_name(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    *,
    parent_worker: str | None = None,
) -> str:
    exit_code, payload = _spawn(
        [_spec(repo, parent_worker=parent_worker)], monkeypatch, capsys
    )
    assert exit_code == 0
    name: str = payload["succeeded"][0]["worker"]["name"]
    return name


def test_root_spawn_creates_a_cwd_keyed_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    monkeypatch.chdir(tmp_path)

    _spawn_and_get_name(tmp_path, monkeypatch, capsys)

    shard_path = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(tmp_path))
    )
    assert shard_path.exists()
    assert len(Registry(shard_path).list()) == 1


def test_root_spawn_reuses_the_same_shard_across_different_cwds_in_one_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The acceptance criterion the card names explicitly: a session that
    ROOT-spawns from more than one cwd must not split its ordinal
    namespace across shards, or a later spawn could re-mint an
    already-claimed name."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    cwd_one = tmp_path / "repo-one"
    cwd_two = tmp_path / "repo-two"
    cwd_one.mkdir()
    cwd_two.mkdir()

    monkeypatch.chdir(cwd_one)
    name_one = _spawn_and_get_name(cwd_one, monkeypatch, capsys)
    monkeypatch.chdir(cwd_two)
    name_two = _spawn_and_get_name(cwd_two, monkeypatch, capsys)

    assert name_one != name_two
    shard_for_cwd_one = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(cwd_one))
    )
    # Only the first cwd's shard exists -- the second spawn reused it
    # instead of minting a new shard keyed off `cwd_two`.
    assert shard_for_cwd_one.exists()
    assert not registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(cwd_two))
    ).exists()
    assert {w.name for w in Registry(shard_for_cwd_one).list()} == {name_one, name_two}


def test_nested_spawn_inherits_the_named_parents_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    monkeypatch.chdir(tmp_path)
    parent_name = _spawn_and_get_name(tmp_path, monkeypatch, capsys)

    # A nested spawn call from a *different* session (as if issued by the
    # parent worker's own shell) but the *same* cwd/worktree, naming its
    # own worker as the parent. This "nested-session" has no records of
    # its own anywhere yet, so the cross-shard grafting guard does not
    # fire regardless of cwd.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "nested-session")
    child_name = _spawn_and_get_name(
        tmp_path, monkeypatch, capsys, parent_worker=parent_name
    )

    parent_shard = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(tmp_path))
    )
    assert child_name in {w.name for w in Registry(parent_shard).list()}


def test_nested_spawn_from_a_different_worktree_and_record_less_session_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The standard documented grandchild flow: a worker's own worktree
    very often differs from the ROOT orchestrator's cwd (a design-time
    probe found this in ~41% of real sessions). A record-less calling
    session (fresh
    `$CLAUDE_CODE_SESSION_ID`, never spawned under before) naming an
    existing `parent_worker` that lives in a *different* shard must be
    ALLOWED -- landing in the parent's shard, no new shard created for
    the caller's own (different) cwd -- because a record-less session has
    not split across shards; it simply hasn't picked one yet."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    monkeypatch.chdir(tmp_path)
    parent_name = _spawn_and_get_name(tmp_path, monkeypatch, capsys)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "nested-session")
    nested_cwd = tmp_path / "nested-worktree"
    nested_cwd.mkdir()
    monkeypatch.chdir(nested_cwd)
    child_name = _spawn_and_get_name(
        nested_cwd, monkeypatch, capsys, parent_worker=parent_name
    )

    parent_shard = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(tmp_path))
    )
    assert child_name in {w.name for w in Registry(parent_shard).list()}
    assert not registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(nested_cwd))
    ).exists()


def test_nested_spawn_from_a_different_worktree_is_rejected_as_cross_shard_grafting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Cross-shard grafting guard: a calling session that *already has its
    own records* in a different shard than the named `parent_worker` must be
    rejected -- pane operations zero, no registry write -- rather than
    silently grafting onto the unrelated tree. (A caller with no prior
    records anywhere is the standard grandchild flow and must NOT be
    rejected -- see `test_nested_spawn_inherits_the_named_parents_shard`
    above.)"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    monkeypatch.chdir(tmp_path)
    parent_name = _spawn_and_get_name(tmp_path, monkeypatch, capsys)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "unrelated-session")
    other_cwd = tmp_path / "unrelated-worktree"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    # "unrelated-session" already has its own, unrelated record in its own
    # shard from an earlier ROOT spawn.
    _spawn_and_get_name(other_cwd, monkeypatch, capsys)
    spawn_calls_before = sum(
        1
        for instance in RecordingBackend.instances
        for call in instance.calls
        if call["operation"] == "spawn"
    )

    exit_code, payload = _spawn(
        [_spec(other_cwd, parent_worker=parent_name)], monkeypatch, capsys
    )

    assert exit_code == 2
    assert payload["ok"] is False
    spawn_calls_after = sum(
        1
        for instance in RecordingBackend.instances
        for call in instance.calls
        if call["operation"] == "spawn"
    )
    assert spawn_calls_after == spawn_calls_before
    parent_shard = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(tmp_path))
    )
    assert len(Registry(parent_shard).list()) == 1
    other_shard = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(other_cwd))
    )
    assert len(Registry(other_shard).list()) == 1


def test_cross_shard_grafting_rejection_is_batch_wide_not_per_element(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """This guard is a batch-wide precondition failure (same posture as
    the purge fix), not a per-element `failed` downgrade. The rejecting
    session must already have records in another shard --
    established here via an earlier, separate ROOT spawn -- rather than
    being record-less and sharing a batch with a ROOT element (that
    combination is a distinct, still-open corner case, not this guard's
    trigger). This puts a *valid* ROOT element
    first and the grafting element second in one call: if the guard only
    downgraded the second element, the first would still show up as a
    `succeeded` spawn reusing the caller's existing shard. It must not --
    the whole batch is rejected before either element resolves a
    registry."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    monkeypatch.chdir(tmp_path)
    parent_name = _spawn_and_get_name(tmp_path, monkeypatch, capsys)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "unrelated-session")
    other_cwd = tmp_path / "unrelated-worktree"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    # "unrelated-session" already has its own shard from an earlier ROOT
    # spawn -- reuse-first would route the batch's own ROOT element back
    # there too.
    _spawn_and_get_name(other_cwd, monkeypatch, capsys)
    other_shard = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(other_cwd))
    )
    records_before = len(Registry(other_shard).list())

    exit_code, payload = _spawn(
        [_spec(other_cwd), _spec(other_cwd, parent_worker=parent_name)],
        monkeypatch,
        capsys,
    )

    assert exit_code == 2
    assert payload["ok"] is False
    # The valid ROOT element (first in the batch, would have reused
    # `other_shard` via reuse-first) never spawned either.
    assert len(Registry(other_shard).list()) == records_before
    parent_shard = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(tmp_path))
    )
    assert len(Registry(parent_shard).list()) == 1


def test_root_and_grandchild_from_a_record_less_session_colliding_in_one_batch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A record-less session issues a ROOT spec and a grandchild spec
    (naming an existing parent that lives in a *different* shard) in the
    *same* batch. Each resolves to its own shard that is empty for *this*
    session and independently computes ordinal=1 -- the same name --
    entirely undetected by `check_cross_shard_grafting()` (a record-less
    caller is that guard's documented no-op case, see
    `test_nested_spawn_from_a_different_
    worktree_and_record_less_session_is_allowed` above). Layer 1 must
    reject the whole batch before any pane operation or worker-record
    write."""
    dir_parent = tmp_path / "parent-worktree"
    dir_parent.mkdir()
    dir_root = tmp_path / "root-worktree"
    dir_root.mkdir()

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-a")
    monkeypatch.chdir(dir_parent)
    parent_name = _spawn_and_get_name(dir_parent, monkeypatch, capsys)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "record-less-session")
    monkeypatch.chdir(dir_root)
    spawn_calls_before = len(RecordingBackend.instances)

    exit_code, payload = _spawn(
        [_spec(dir_parent), _spec(dir_parent, parent_worker=parent_name)],
        monkeypatch,
        capsys,
    )

    assert exit_code == 2
    assert payload["ok"] is False
    assert len(RecordingBackend.instances) == spawn_calls_before
    parent_shard = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(dir_parent))
    )
    assert [w.name for w in Registry(parent_shard).list()] == [parent_name]
    root_shard = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(dir_root))
    )
    assert Registry(root_shard).list() == []


@dataclass(frozen=True)
class _ParentSpawn:
    """A real ROOT worker's name plus the shard file it landed in --
    parameter-object return value so the sister-form test below stays
    under the layer's local-variable threshold."""

    name: str
    shard_path: Path


def _spawn_root_and_get_name_and_shard(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    *,
    session_id: str,
) -> _ParentSpawn:
    directory.mkdir()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    monkeypatch.chdir(directory)
    name = _spawn_and_get_name(directory, monkeypatch, capsys)
    shard_path = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(directory))
    )
    return _ParentSpawn(name=name, shard_path=shard_path)


def test_two_grandchildren_of_different_shards_from_a_record_less_session_colliding_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The card's own named "sister form" of the same gap: no ROOT element
    at all -- a record-less session issues two *grandchild* specs in one
    batch, each naming an existing parent that lives in a different shard.
    Both resolve without tripping `check_cross_shard_grafting()` (the
    calling session has no records anywhere at either resolution, since
    resolving a batch's specs never itself commits anything), and each
    shard independently computes ordinal=1 for the same repo/role -- same
    collision, different shape. Must also be rejected batch-wide.

    Distinct first-hyphen-segment tokens ("alpha"/"beta") for the two real
    parent-owning sessions -- `worker_name()`'s token is only that first
    segment, so sharing one (e.g. both starting "session-...") would make
    these two ordinary, unrelated spawns collide with *each other* via
    Layer 1's persisted-name check, before the batch under test even
    runs."""
    parent_a = _spawn_root_and_get_name_and_shard(
        tmp_path / "dir-a", monkeypatch, capsys, session_id="alpha-session"
    )
    parent_b = _spawn_root_and_get_name_and_shard(
        tmp_path / "dir-b", monkeypatch, capsys, session_id="beta-session"
    )
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "record-less-session")
    spawn_calls_before = len(RecordingBackend.instances)

    exit_code, payload = _spawn(
        [
            _spec(tmp_path / "dir-a", parent_worker=parent_a.name),
            _spec(tmp_path / "dir-a", parent_worker=parent_b.name),
        ],
        monkeypatch,
        capsys,
    )

    assert exit_code == 2
    assert payload["ok"] is False
    assert len(RecordingBackend.instances) == spawn_calls_before
    assert [w.name for w in Registry(parent_a.shard_path).list()] == [parent_a.name]
    assert [w.name for w in Registry(parent_b.shard_path).list()] == [parent_b.name]


def test_spawn_rejects_a_name_already_persisted_in_a_shard_this_batch_never_touches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Layer 1's "batch-external" collision (called out separately from
    the in-batch case): the candidate name a fresh session's ROOT spawn
    computes already exists in
    some *other* shard this batch's own specs never resolve to or touch.
    Reachable because `worker_name()`'s token is only the first
    `-`-delimited segment of `parent_session_id` -- two distinct session
    ids can share that segment (crafted here to make the collision
    reproducible) while remaining fully independent for ordinal/shard-reuse
    accounting, so nothing about resolving *this* batch's own spec would
    ever surface the other session's shard."""
    dir_x = tmp_path / "dir-x"
    dir_x.mkdir()
    dir_y = tmp_path / "dir-y"
    dir_y.mkdir()

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abcdef12-alpha")
    monkeypatch.chdir(dir_x)
    existing_name = _spawn_and_get_name(dir_x, monkeypatch, capsys)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abcdef12-beta")
    monkeypatch.chdir(dir_y)
    spawn_calls_before = len(RecordingBackend.instances)

    exit_code, payload = _spawn([_spec(dir_x)], monkeypatch, capsys)

    assert exit_code == 2
    assert payload["ok"] is False
    assert len(RecordingBackend.instances) == spawn_calls_before
    shard_y = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(dir_y))
    )
    assert Registry(shard_y).list() == []
    shard_x = registry_shard_module.shard_path_for_key(
        encode_project_dir_name(str(dir_x))
    )
    assert [w.name for w in Registry(shard_x).list()] == [existing_name]


def test_close_and_state_resolve_a_worker_by_name_across_shards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    monkeypatch.chdir(tmp_path)
    _, payload = _spawn([_spec(tmp_path)], monkeypatch, capsys)
    name = payload["succeeded"][0]["worker"]["name"]

    for target_state in ("busy", "reported"):
        assert main(["state", "--worker", name, "--state", target_state]) == 0
    capsys.readouterr()
    assert main(["accept", "--worker", name]) == 0
    capsys.readouterr()
    assert main(["close", "--worker", name]) == 0

    result = json.loads(capsys.readouterr().out)
    from sukuna.domain.entity.worker_record import WorkerState

    assert result["state"] == WorkerState.CLOSED.value


def test_inspect_with_no_worker_merges_every_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    shard_a = Registry(registry_shard_module.shard_path_for_key("a"))
    shard_a.add(_make_worker(tmp_path, name="ccw-a"))
    shard_b = Registry(registry_shard_module.shard_path_for_key("b"))
    shard_b.add(_make_worker(tmp_path, name="ccw-b"))

    assert main(["inspect"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert {w["name"] for w in payload["workers"]} == {"ccw-a", "ccw-b"}


def test_tree_merges_every_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    shard_a = Registry(registry_shard_module.shard_path_for_key("a"))
    shard_a.add(_make_worker(tmp_path, name="ccw-a", parent_session_id="session-a"))
    shard_b = Registry(registry_shard_module.shard_path_for_key("b"))
    shard_b.add(_make_worker(tmp_path, name="ccw-b", parent_session_id="session-b"))

    assert main_cli(["tree", "--text"]) == 0
    output = capsys.readouterr().out

    assert "ccw-a" in output
    assert "ccw-b" in output


def test_reconcile_fans_out_across_every_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from sukuna.domain.entity.worker_record import WorkerState

    shard_a = Registry(registry_shard_module.shard_path_for_key("a"))
    worker_a = _make_worker(tmp_path, name="ccw-a", pane_ref="%1")
    worker_a.state = WorkerState.FAILED
    shard_a.add(worker_a)
    shard_b = Registry(registry_shard_module.shard_path_for_key("b"))
    worker_b = _make_worker(tmp_path, name="ccw-b", pane_ref="%2")
    worker_b.state = WorkerState.TIMED_OUT
    shard_b.add(worker_b)

    assert main(["reconcile"]) == 0
    payload = json.loads(capsys.readouterr().out)

    cleared_names = {entry["name"] for entry in payload["pane_cleared"]}
    assert cleared_names == {"ccw-a", "ccw-b"}


def test_migration_runs_transparently_on_first_cli_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    legacy = registry_shard_module.legacy_registry_path()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    worker = _make_worker(tmp_path, name="ccw-legacy", parent_session_id=None)
    legacy.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "workers": [record_to_dict(worker)],
                "ordinal_high_water_marks": {},
                "last_swept_at": None,
            }
        ),
        encoding="utf-8",
    )

    assert main(["inspect"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert {w["name"] for w in payload["workers"]} == {"ccw-legacy"}
    assert not legacy.exists()
    assert registry_shard_module.legacy_backup_path().exists()


def test_two_different_shards_have_independent_locks_that_do_not_interfere(
    tmp_path: Path,
) -> None:
    """Shard-level locking (card): holding shard A's lock must never block
    an operation on shard B."""
    shard_a = Registry(registry_shard_module.shard_path_for_key("a"))
    shard_b = Registry(registry_shard_module.shard_path_for_key("b"))
    assert shard_a.lock_path != shard_b.lock_path

    shard_a.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with shard_a.lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            # Shard B's own operation must complete immediately, not block
            # on shard A's held lock.
            shard_b.add(_make_worker(tmp_path, name="ccw-in-b"))
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    assert {w.name for w in shard_b.list()} == {"ccw-in-b"}

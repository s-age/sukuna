"""`usecase/registry_access.py`: the orchestration wiring shard resolution
(infra + domain) to the existing, unmodified per-worker usecase functions
-- by-name resolution, the spawn-batch resolver, the merged read-only view,
and reconcile's per-shard fan-out."""

from __future__ import annotations

from pathlib import Path

import pytest
from _terminal_fakes import RecordingBackend, install_backends
from _terminal_fakes import make_worker as _make_worker

import sukuna.infrastructure.registry_shard as registry_shard_module
from sukuna.domain.entity.worker_record import WorkerState
from sukuna.domain.service.spawn_preflight import SpawnSpec
from sukuna.errors import ValidationError
from sukuna.infrastructure.registry import Registry
from sukuna.usecase import registry_access

_REPO = Path("/repo")


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


def _spec(*, parent_worker: str | None = None) -> SpawnSpec:
    return SpawnSpec(
        role="review",
        repo=_REPO,
        worktree=_REPO,
        goal=None,
        parent_worker=parent_worker,
        active_pane_width=50,
        model=None,
    )


def test_registry_for_worker_finds_the_shard_holding_the_name() -> None:
    shard = Registry(registry_shard_module.shard_path_for_key("a"))
    shard.add(_make_worker(_REPO, name="ccw-a"))

    resolved = registry_access.registry_for_worker("ccw-a")

    assert resolved.path == shard.path


def test_registry_for_worker_falls_back_to_the_catch_all_shard_for_an_unknown_name() -> (
    None
):
    resolved = registry_access.registry_for_worker("ccw-unknown")

    assert resolved.path == registry_shard_module.shard_path_for_key(
        registry_shard_module.CATCH_ALL_SHARD_KEY
    )


def test_spawn_registry_resolver_routes_a_root_spec_by_session_and_cwd() -> None:
    resolve = registry_access.spawn_registry_resolver(
        parent_session_id="s1", cwd="/some/cwd"
    )

    registry = resolve(_spec())

    assert (
        registry.path
        == registry_access.registry_for_root(
            parent_session_id="s1", cwd="/some/cwd"
        ).path
    )


def test_spawn_registry_resolver_routes_a_child_spec_to_its_named_parents_shard() -> (
    None
):
    """The legitimate nested-spawn case: the calling session already owns
    the parent's shard (reuse-first finds it via `parent_session_id`), so
    the cross-shard grafting guard does not fire."""
    parent_registry = registry_access.registry_for_root(
        parent_session_id="s1", cwd="/cwd"
    )
    parent_registry.add(_make_worker(_REPO, name="ccw-parent", parent_session_id="s1"))
    resolve = registry_access.spawn_registry_resolver(
        parent_session_id="s1", cwd="/different-cwd"
    )

    registry = resolve(_spec(parent_worker="ccw-parent"))

    assert registry.path == parent_registry.path


def test_spawn_registry_resolver_rejects_cross_shard_grafting_for_a_child_spec() -> (
    None
):
    """Cross-shard grafting guard: a resolver whose own session *already
    has records in a different shard* than the named `parent_worker`'s
    actual shard must
    reject the spec, not silently graft it onto the unrelated tree. (A
    record-less "s2" would be the standard grandchild flow and must NOT be
    rejected -- see `test_spawn_registry_resolver_routes_a_child_spec_to_
    its_named_parents_shard` above.)"""
    parent_registry = registry_access.registry_for_root(
        parent_session_id="s1", cwd="/cwd"
    )
    parent_registry.add(_make_worker(_REPO, name="ccw-parent", parent_session_id="s1"))
    # "s2" already has its own, unrelated record in a different shard.
    registry_access.registry_for_root(parent_session_id="s2", cwd="/unrelated-cwd").add(
        _make_worker(_REPO, name="ccw-unrelated", parent_session_id="s2")
    )
    resolve = registry_access.spawn_registry_resolver(
        parent_session_id="s2", cwd="/different-cwd"
    )

    with pytest.raises(ValidationError):
        resolve(_spec(parent_worker="ccw-parent"))


def test_all_workers_view_merges_every_shard() -> None:
    Registry(registry_shard_module.shard_path_for_key("a")).add(
        _make_worker(_REPO, name="ccw-a")
    )
    Registry(registry_shard_module.shard_path_for_key("b")).add(
        _make_worker(_REPO, name="ccw-b")
    )

    view = registry_access.all_workers_view()

    assert {w.name for w in view.list()} == {"ccw-a", "ccw-b"}


def test_reconcile_all_shards_merges_results_from_every_real_shard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_backends(
        monkeypatch, {"tmux": RecordingBackend, "iterm2": RecordingBackend}
    )
    shard_a = Registry(registry_shard_module.shard_path_for_key("a"))
    worker_a = _make_worker(_REPO, name="ccw-a", pane_ref="%1")
    worker_a.state = WorkerState.FAILED
    shard_a.add(worker_a)
    shard_b = Registry(registry_shard_module.shard_path_for_key("b"))
    worker_b = _make_worker(_REPO, name="ccw-b", pane_ref="%2")
    worker_b.state = WorkerState.TIMED_OUT
    shard_b.add(worker_b)

    result = registry_access.reconcile_all_shards()

    cleared_names = {entry["name"] for entry in result["pane_cleared"]}
    assert cleared_names == {"ccw-a", "ccw-b"}
    assert Registry(shard_a.path).get("ccw-a").pane_ref is None
    assert Registry(shard_b.path).get("ccw-b").pane_ref is None
    assert result["duplicate_names"] == []


def test_reconcile_all_shards_reports_a_name_present_in_two_shards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer 2 of the cross-shard name-uniqueness validation: a `name`
    that somehow ended up committed to two different shard files (a
    Layer 1 TOCTOU race, or a hand-edited registry) must be reported
    under `duplicate_names`, and neither copy may be
    renamed or removed -- `name` is also a live `claude -n` session
    identifier; no automatic "pick a winner" repair is safe."""
    install_backends(
        monkeypatch, {"tmux": RecordingBackend, "iterm2": RecordingBackend}
    )
    shard_a = Registry(registry_shard_module.shard_path_for_key("a"))
    dup_a = _make_worker(_REPO, name="ccw-dup", pane_ref="%1")
    shard_a.add(dup_a)
    shard_b = Registry(registry_shard_module.shard_path_for_key("b"))
    dup_b = _make_worker(_REPO, name="ccw-dup", pane_ref="%2")
    shard_b.add(dup_b)
    # Both panes verify as alive (RecordingBackend's fake `verify` reports
    # `exists: pane_ref in world`), so neither of the two reconcile passes
    # above mutates either record -- isolates this assertion to Layer 2's
    # own detect-only behavior.
    RecordingBackend.world.update({"%1", "%2"})

    result = registry_access.reconcile_all_shards()

    assert len(result["duplicate_names"]) == 1
    (report,) = result["duplicate_names"]
    assert report["name"] == "ccw-dup"
    reported_shard_paths = {worker["shard_path"] for worker in report["workers"]}
    assert reported_shard_paths == {str(shard_a.path), str(shard_b.path)}
    # Detection only -- both copies must still exist afterward, untouched
    # in every field the reconcile passes above didn't already legitimately
    # change (both are READY with a live pane_ref, so neither pass mutates
    # them; this just confirms Layer 2 itself never writes).
    assert Registry(shard_a.path).get("ccw-dup").pane_ref == "%1"
    assert Registry(shard_b.path).get("ccw-dup").pane_ref == "%2"

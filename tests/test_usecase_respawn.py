from pathlib import Path
from typing import Any, ClassVar, TypedDict, Unpack

import pytest
from _terminal_fakes import (
    ITERM2_PANE,
    TMUX_PANE,
    OtherBackend,
    RecordingBackend,
    install_backends,
    patch_pane_resolution,
)
from _terminal_fakes import (
    make_worker as _make_worker,
)

import sukuna.infrastructure.terminal.operations as operations_module
import sukuna.usecase.respawn as respawn_module
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.domain.mapper.command_mapper import resume_command
from sukuna.errors import BackendError, ConflictError, NotFoundError, ValidationError
from sukuna.infrastructure.registry import Registry
from sukuna.usecase.respawn import respawn


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch):
    install_backends(monkeypatch, {"tmux": RecordingBackend, "iterm2": OtherBackend})
    patch_pane_resolution(monkeypatch, respawn_module)
    # `respawn()` calls `load_active_pane_width()` with no override path
    # (unlike spawn's preflight-resolved spec field) -- pin it so tests
    # never read the real host's setting.toml.
    monkeypatch.setattr(respawn_module, "load_active_pane_width", lambda: 50)
    yield


class _WorkerOverrides(TypedDict, total=False):
    parent_session_id: str | None
    parent_worker_name: str | None
    worktree: Path | None
    managed: bool
    goal: str | None
    model: str | None
    session_log_path: str | None
    session_log_reset_at: str | None


def make_worker(
    tmp_path: Path,
    *,
    name: str,
    state: WorkerState,
    pane_ref: str | None,
    **overrides: Unpack[_WorkerOverrides],
) -> WorkerRecord:
    """`state` is accepted (mirroring the original factory's signature) but
    deliberately unused -- every worker is created via `WorkerRecord.create`
    (always `STARTING`); callers reach their intended state afterward via
    `_reach()`'s `registry.transition()` calls. Keeping the unused kwarg
    preserves call sites that pass `state=` as self-documentation."""
    overrides.setdefault("parent_session_id", "old-session")
    overrides.setdefault("goal", "review the thing")
    overrides.setdefault("model", "claude-opus-5")
    return _make_worker(tmp_path, name=name, pane_ref=pane_ref, **overrides)


def _reach(registry: Registry, worker: WorkerRecord, states: list[WorkerState]) -> None:
    for state in states:
        registry.transition(worker.name, state)


def spawn_calls(backend_cls: type[RecordingBackend] = RecordingBackend) -> list[dict]:
    return [
        call
        for instance in backend_cls.instances
        for call in instance.calls
        if call["operation"] == "spawn"
    ]


def equalize_calls(
    backend_cls: type[RecordingBackend] = RecordingBackend,
) -> list[dict]:
    """tmux equalize is a `pane_heights` read plus `set_pane_height` writes
    assembled by `operations.py`; iTerm2 keeps the one-shot `equalize` op.
    Either marker fires exactly once per equalize and carries the
    column."""
    return [
        call
        for instance in backend_cls.instances
        for call in instance.calls
        if call["operation"] in ("pane_heights", "equalize")
    ]


# -- eligibility (R1) --


def test_respawn_succeeds_from_closed_with_no_pane(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, name="ccw-x-review-a", state=WorkerState.STARTING, pane_ref=None
    )
    registry.add(worker)
    _reach(
        registry,
        worker,
        [
            WorkerState.READY,
            WorkerState.BUSY,
            WorkerState.REPORTED,
            WorkerState.ACCEPTED,
            WorkerState.CLOSED,
        ],
    )

    result = respawn(registry, worker.name, parent_session_id="current-session")

    assert result["state"] == WorkerState.READY.value
    assert result["pane_ref"] == TMUX_PANE.format(1)
    updated = registry.get(worker.name)
    assert updated.state is WorkerState.READY
    assert updated.pane_ref == TMUX_PANE.format(1)


@pytest.mark.parametrize("state", [WorkerState.FAILED, WorkerState.TIMED_OUT])
def test_respawn_succeeds_from_failed_or_timed_out_with_no_pane(
    tmp_path: Path, state: WorkerState
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, name="ccw-x-review-a", state=WorkerState.STARTING, pane_ref=None
    )
    registry.add(worker)
    _reach(registry, worker, [state])

    result = respawn(registry, worker.name, parent_session_id="current-session")

    assert result["state"] == WorkerState.READY.value


def test_respawn_rejects_a_worker_with_an_attached_pane_and_names_reconcile(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path,
        name="ccw-x-review-a",
        state=WorkerState.STARTING,
        pane_ref=TMUX_PANE.format(9),
    )
    registry.add(worker)
    _reach(registry, worker, [WorkerState.READY])

    with pytest.raises(ValidationError, match="reconcile"):
        respawn(registry, worker.name, parent_session_id="current-session")

    assert RecordingBackend.instances == []
    assert registry.get(worker.name).state is WorkerState.READY


@pytest.mark.parametrize(
    "state",
    [
        WorkerState.STARTING,
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
    ],
)
def test_respawn_rejects_a_non_terminal_worker_even_without_a_pane(
    tmp_path: Path, state: WorkerState
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, name="ccw-x-review-a", state=WorkerState.STARTING, pane_ref=None
    )
    registry.add(worker)
    progression = [
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
    ]
    if state is not WorkerState.STARTING:
        _reach(registry, worker, progression[: progression.index(state) + 1])

    with pytest.raises(ValidationError):
        respawn(registry, worker.name, parent_session_id="current-session")

    assert RecordingBackend.instances == []


def test_respawn_rejects_an_unmanaged_worker(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path,
        name="ccw-x-review-a",
        state=WorkerState.STARTING,
        pane_ref=None,
        managed=False,
    )
    registry.add(worker)
    _reach(registry, worker, [WorkerState.FAILED])

    with pytest.raises(ValidationError):
        respawn(registry, worker.name, parent_session_id="current-session")

    assert RecordingBackend.instances == []


def test_respawn_raises_not_found_for_an_unregistered_name(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")

    with pytest.raises(NotFoundError):
        respawn(registry, "ccw-x-review-ghost", parent_session_id="current-session")


# -- backend mismatch (cross-session guard, section 3) --


def test_respawn_rejects_backend_mismatch_against_the_current_session_before_any_write(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    other_backend_worker = make_worker(
        tmp_path,
        name="ccw-x-review-live",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="current-session",
    )
    registry.add(other_backend_worker)
    _reach(registry, other_backend_worker, [WorkerState.READY])
    registry.mutate(
        other_backend_worker.name,
        lambda w: w.attach_pane(ITERM2_PANE.format(0), None),
    )
    target = make_worker(
        tmp_path, name="ccw-x-review-a", state=WorkerState.STARTING, pane_ref=None
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    before = registry.get(target.name).updated_at

    with pytest.raises(ValidationError):
        respawn(registry, target.name, parent_session_id="current-session")

    assert RecordingBackend.instances == []
    assert OtherBackend.instances == []
    assert registry.get(target.name).updated_at == before
    assert registry.get(target.name).state is WorkerState.FAILED


# -- placement: R4 "reopen in the original lane" --


def test_respawn_nests_under_a_still_living_parent(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    parent = make_worker(
        tmp_path,
        name="ccw-x-review-parent",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(parent)
    _reach(registry, parent, [WorkerState.READY])
    registry.mutate(parent.name, lambda w: w.attach_pane(TMUX_PANE.format(1), "win-p"))
    sibling = make_worker(
        tmp_path,
        name="ccw-x-review-sibling",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
        parent_worker_name=parent.name,
    )
    registry.add(sibling)
    _reach(registry, sibling, [WorkerState.READY])
    registry.mutate(sibling.name, lambda w: w.attach_pane(TMUX_PANE.format(2), "win-s"))
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
        parent_worker_name=parent.name,
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    RecordingBackend.world = {
        "orchestrator-pane",
        TMUX_PANE.format(1),
        TMUX_PANE.format(2),
    }

    respawn(registry, target.name, parent_session_id="current-session")

    call = spawn_calls()[-1]
    assert call["anchor_pane_ref"] == TMUX_PANE.format(2)
    assert call["split_direction"] == "vertical"
    assert len(equalize_calls()) == 1
    assert equalize_calls()[0]["column_pane_refs"] == [
        TMUX_PANE.format(2),
        TMUX_PANE.format(
            1
        ),  # the newly respawned pane (only spawn() call in this test)
    ]
    # R2: parent_session_id / parent_worker_name are never rewritten.
    updated = registry.get(target.name)
    assert updated.parent_session_id == "old-session"
    assert updated.parent_worker_name == parent.name


def test_respawn_falls_back_to_the_old_sessions_root_when_the_parent_is_gone(
    tmp_path: Path,
) -> None:
    """R5: a dead parent doesn't block respawn -- it degrades straight to
    the old session's root column, skipping the parent entirely."""
    registry = Registry(tmp_path / "registry.json")
    root_survivor = make_worker(
        tmp_path,
        name="ccw-x-review-root",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(root_survivor)
    _reach(registry, root_survivor, [WorkerState.READY])
    registry.mutate(
        root_survivor.name, lambda w: w.attach_pane(TMUX_PANE.format(1), "win-r")
    )
    dead_parent = make_worker(
        tmp_path,
        name="ccw-x-review-parent",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(dead_parent)
    _reach(
        registry,
        dead_parent,
        [
            WorkerState.READY,
            WorkerState.BUSY,
            WorkerState.REPORTED,
            WorkerState.ACCEPTED,
            WorkerState.CLOSED,
        ],
    )
    orphan = make_worker(
        tmp_path,
        name="ccw-x-review-orphan",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
        parent_worker_name=dead_parent.name,
    )
    registry.add(orphan)
    _reach(registry, orphan, [WorkerState.FAILED])
    RecordingBackend.world = {"orchestrator-pane", TMUX_PANE.format(1)}

    respawn(registry, orphan.name, parent_session_id="current-session")

    call = spawn_calls()[-1]
    assert call["anchor_pane_ref"] == TMUX_PANE.format(1)
    assert call["split_direction"] == "vertical"


def test_respawn_of_two_orphan_siblings_each_splits_the_orchestrator_pane(
    tmp_path: Path,
) -> None:
    """A dead-parent lineage is NOT reunited across respawns --
    `root_children()` never picks up a worker whose `parent_worker_name`
    is set, so a second orphan sibling doesn't see the first one's
    freshly respawned pane. This is the documented, accepted degrade, not
    a bug."""
    registry = Registry(tmp_path / "registry.json")
    dead_parent = make_worker(
        tmp_path,
        name="ccw-x-review-parent",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(dead_parent)
    _reach(
        registry,
        dead_parent,
        [
            WorkerState.READY,
            WorkerState.BUSY,
            WorkerState.REPORTED,
            WorkerState.ACCEPTED,
            WorkerState.CLOSED,
        ],
    )
    first = make_worker(
        tmp_path,
        name="ccw-x-review-first",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
        parent_worker_name=dead_parent.name,
    )
    registry.add(first)
    _reach(registry, first, [WorkerState.FAILED])
    second = make_worker(
        tmp_path,
        name="ccw-x-review-second",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
        parent_worker_name=dead_parent.name,
    )
    registry.add(second)
    _reach(registry, second, [WorkerState.FAILED])
    RecordingBackend.world = {"orchestrator-pane"}

    respawn(registry, first.name, parent_session_id="current-session")
    respawn(registry, second.name, parent_session_id="current-session")

    calls = spawn_calls()
    assert calls[-2]["anchor_pane_ref"] == "orchestrator-pane"
    assert calls[-2]["split_direction"] == "horizontal"
    assert calls[-1]["anchor_pane_ref"] == "orchestrator-pane"
    assert calls[-1]["split_direction"] == "horizontal"


def test_respawn_of_a_root_worker_stacks_under_the_old_sessions_surviving_root_column(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    survivor = make_worker(
        tmp_path,
        name="ccw-x-review-survivor",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(survivor)
    _reach(registry, survivor, [WorkerState.READY])
    registry.mutate(
        survivor.name, lambda w: w.attach_pane(TMUX_PANE.format(1), "win-s")
    )
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    RecordingBackend.world = {"orchestrator-pane", TMUX_PANE.format(1)}

    respawn(registry, target.name, parent_session_id="current-session")

    call = spawn_calls()[-1]
    assert call["anchor_pane_ref"] == TMUX_PANE.format(1)
    assert call["split_direction"] == "vertical"


def test_respawn_of_a_root_worker_splits_the_orchestrator_pane_when_the_old_root_column_is_empty(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    RecordingBackend.world = {"orchestrator-pane"}

    respawn(registry, target.name, parent_session_id="current-session")

    call = spawn_calls()[-1]
    assert call["anchor_pane_ref"] == "orchestrator-pane"
    assert call["split_direction"] == "horizontal"
    # Only the "0 children -> horizontal split" case forwards active_pane_width.
    resize_calls = [
        c
        for instance in RecordingBackend.instances
        for c in instance.calls
        if c["operation"] == "resize"
    ]
    assert len(resize_calls) == 1


def test_respawn_ignores_active_pane_width_when_stacking_vertically(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    survivor = make_worker(
        tmp_path,
        name="ccw-x-review-survivor",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(survivor)
    _reach(registry, survivor, [WorkerState.READY])
    registry.mutate(
        survivor.name, lambda w: w.attach_pane(TMUX_PANE.format(1), "win-s")
    )
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    RecordingBackend.world = {"orchestrator-pane", TMUX_PANE.format(1)}

    respawn(registry, target.name, parent_session_id="current-session")

    resize_calls = [
        c
        for instance in RecordingBackend.instances
        for c in instance.calls
        if c["operation"] == "resize"
    ]
    assert resize_calls == []


# -- fallback / anchor-vanished / backend-shape checks (section 2.2/2.3) --


def test_respawn_falls_back_once_when_the_chosen_anchor_no_longer_exists(
    tmp_path: Path,
) -> None:
    root_survivor_pane = TMUX_PANE.format(1)
    dead_parent_pane = TMUX_PANE.format(2)
    registry = Registry(tmp_path / "registry.json")
    root_survivor = make_worker(
        tmp_path,
        name="ccw-x-review-root",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(root_survivor)
    _reach(registry, root_survivor, [WorkerState.READY])
    registry.mutate(
        root_survivor.name, lambda w: w.attach_pane(root_survivor_pane, "win-r")
    )
    named_parent = make_worker(
        tmp_path,
        name="ccw-x-review-parent",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
        parent_worker_name=root_survivor.name,
    )
    registry.add(named_parent)
    _reach(registry, named_parent, [WorkerState.READY])
    registry.mutate(
        named_parent.name, lambda w: w.attach_pane(dead_parent_pane, "win-p")
    )
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
        parent_worker_name=named_parent.name,
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    # named_parent nests under root_survivor, and its own pane is not part
    # of `world` -- it was killed outside sukuna's knowledge -- while
    # root_survivor's own pane is still alive, so the root fallback anchor
    # (root_survivor) differs from the dead child-scope anchor (named_parent).
    RecordingBackend.world = {"orchestrator-pane", root_survivor_pane}

    respawn(registry, target.name, parent_session_id="current-session")

    call = spawn_calls()[-1]
    assert call["anchor_pane_ref"] == root_survivor_pane
    assert call["split_direction"] == "vertical"


def test_respawn_fails_and_marks_failed_when_no_anchor_survives_even_the_fallback(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    RecordingBackend.world = set()  # orchestrator pane itself is gone too

    with pytest.raises(ValidationError):
        respawn(registry, target.name, parent_session_id="current-session")

    assert registry.get(target.name).state is WorkerState.FAILED
    assert registry.get(target.name).pane_ref is None
    assert not spawn_calls()


def test_respawn_rejects_when_the_named_parents_pane_is_a_foreign_backend_shape(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    parent = make_worker(
        tmp_path,
        name="ccw-x-review-parent",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(parent)
    _reach(registry, parent, [WorkerState.READY])
    registry.mutate(
        parent.name, lambda w: w.attach_pane(ITERM2_PANE.format(0), "win-p")
    )
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
        parent_worker_name=parent.name,
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])

    with pytest.raises(ValidationError):
        respawn(registry, target.name, parent_session_id="current-session")

    assert registry.get(target.name).state is WorkerState.FAILED
    assert not spawn_calls()


def test_respawn_rejects_when_the_fallback_root_columns_last_pane_is_a_foreign_backend_shape(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    root_survivor = make_worker(
        tmp_path,
        name="ccw-x-review-root",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(root_survivor)
    _reach(registry, root_survivor, [WorkerState.READY])
    registry.mutate(
        root_survivor.name, lambda w: w.attach_pane(ITERM2_PANE.format(0), "win-r")
    )
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])

    with pytest.raises(ValidationError):
        respawn(registry, target.name, parent_session_id="current-session")

    assert registry.get(target.name).state is WorkerState.FAILED
    assert not spawn_calls()


# -- resume_command shape (F1/F2) --


def test_respawn_bridges_the_jsonl_derived_model_and_permission_mode_into_resume_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that `respawn()` passes through whatever
    `resolve_last_session_state()` derives from the worker's own jsonl."""
    registry = Registry(tmp_path / "registry.json")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
        worktree=worktree,
        model="claude-opus-5",
        session_log_path="/fake/session.jsonl",
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    RecordingBackend.world = {"orchestrator-pane"}
    monkeypatch.setattr(
        respawn_module,
        "resolve_last_session_state",
        lambda session_log_path: ("claude-sonnet-5", "auto"),
    )

    respawn(registry, target.name, parent_session_id="current-session")

    call = spawn_calls()[-1]
    assert call["command"] == resume_command(
        worktree=worktree,
        name=target.name,
        shell="/bin/zsh",
        model="claude-sonnet-5",
        permission_mode="auto",
    )
    assert "--resume" in call["command"]
    assert "-n " not in call["command"]
    assert "--model claude-sonnet-5" in call["command"]
    assert "--permission-mode auto" in call["command"]


def test_respawn_falls_back_to_worker_model_when_no_jsonl_derived_model_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
        worktree=worktree,
        model="claude-opus-5",
        session_log_path="/fake/session.jsonl",
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    RecordingBackend.world = {"orchestrator-pane"}
    monkeypatch.setattr(
        respawn_module,
        "resolve_last_session_state",
        lambda session_log_path: (None, None),
    )

    respawn(registry, target.name, parent_session_id="current-session")

    call = spawn_calls()[-1]
    assert "--model claude-opus-5" in call["command"]
    assert "--permission-mode" not in call["command"]


def test_respawn_omits_both_flags_when_neither_jsonl_nor_worker_model_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
        worktree=worktree,
        model=None,
        session_log_path=None,
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    RecordingBackend.world = {"orchestrator-pane"}
    monkeypatch.setattr(
        respawn_module,
        "resolve_last_session_state",
        lambda session_log_path: (None, None),
    )

    respawn(registry, target.name, parent_session_id="current-session")

    call = spawn_calls()[-1]
    assert "--model" not in call["command"]
    assert "--permission-mode" not in call["command"]


def test_respawn_never_rewrites_lineage_goal_or_model(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
        parent_worker_name=None,
        goal="review the thing",
        model="claude-opus-5",
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    RecordingBackend.world = {"orchestrator-pane"}

    result = respawn(registry, target.name, parent_session_id="current-session")

    assert result["parent_session_id"] == "old-session"
    assert result["parent_worker_name"] is None
    assert result["goal"] == "review the thing"
    assert result["model"] == "claude-opus-5"


# -- worktree pre-flight (design review finding 2) --


def test_respawn_rejects_a_deleted_worktree_before_any_registry_write(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    gone_worktree = tmp_path / "does-not-exist"
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        worktree=gone_worktree,
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    before = registry.get(target.name).updated_at

    with pytest.raises(ValidationError):
        respawn(registry, target.name, parent_session_id="current-session")

    assert RecordingBackend.instances == []
    assert registry.get(target.name).updated_at == before
    assert registry.get(target.name).state is WorkerState.FAILED


# -- final-mutate ConflictError compensation (design review finding 1) --


def test_respawn_final_mutate_conflict_reattaches_the_pane_and_reraises(
    tmp_path: Path, monkeypatch
) -> None:
    """Design review finding 1: a concurrent write landing between the
    STARTING transition and the final READY mutate must not leave the
    freshly created pane orphaned (referenced by no record) -- the
    compensation re-attaches it to whatever the conflicting record now is,
    then re-raises the original ConflictError untouched."""
    registry = Registry(tmp_path / "registry.json")
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])

    class RacingBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "spawn":
                # respawn() already moved `target` to STARTING by this
                # point; race it to FAILED (a valid STARTING exit) right
                # before the final CAS-guarded mutate runs.
                registry.transition(target.name, WorkerState.FAILED)
            return super().run(request)

    monkeypatch.setattr(
        operations_module, "BACKENDS", {"tmux": RacingBackend, "iterm2": OtherBackend}
    )
    RecordingBackend.world = {"orchestrator-pane"}

    with pytest.raises(ConflictError):
        respawn(registry, target.name, parent_session_id="current-session")

    updated = registry.get(target.name)
    assert (
        updated.state is WorkerState.FAILED
    )  # the racing write's own transition survives
    assert updated.pane_ref == TMUX_PANE.format(
        1
    )  # but the orphaned pane got re-attached


# -- session_log_path clear on respawn --


def test_respawn_clears_session_log_path_and_stamps_reset_at_under_one_lock(
    tmp_path: Path,
) -> None:
    """The STARTING transition must clear `session_log_path` and stamp
    `session_log_reset_at` with that same transition's `updated_at`, all
    persisted together via `registry.mutate()` -- not a local-variable
    assignment before a `registry.transition()` call, which `mutate()`'s
    fresh-read-under-lock semantics would silently discard."""
    registry = Registry(tmp_path / "registry.json")
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        session_log_path="/logs/old-session.jsonl",
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])
    RecordingBackend.world = {"orchestrator-pane"}

    respawn(registry, target.name, parent_session_id="current-session")

    updated = registry.get(target.name)
    assert updated.state is WorkerState.READY
    # The general mutate()/replace() hook (default real resolver, no
    # matching jsonl in this test's fake filesystem) leaves it None rather
    # than reinstating the cleared value.
    assert updated.session_log_path is None


def test_respawn_backend_spawn_failure_fails_the_worker_and_leaves_no_pane(
    tmp_path: Path, monkeypatch
) -> None:
    """CLAUDE.md invariant: a backend call failure drives the worker to
    FAILED. The spawn split itself fails here, so unlike a post-spawn
    follow-up failure there is no pane to keep -- pane_ref stays None."""
    registry = Registry(tmp_path / "registry.json")
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])

    class FailingSpawnBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def _spawn(self, request: dict) -> dict:
            return {"ok": False, "error": "split-window failed"}

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": FailingSpawnBackend, "iterm2": OtherBackend},
    )
    RecordingBackend.world = {"orchestrator-pane"}

    with pytest.raises(BackendError):
        respawn(registry, target.name, parent_session_id="current-session")

    updated = registry.get(target.name)
    assert updated.state is WorkerState.FAILED
    assert updated.pane_ref is None


def test_respawn_survives_an_equalize_backend_error_and_reports_a_warning(
    tmp_path: Path, monkeypatch
) -> None:
    """Vertical stacking under the old session's surviving root column makes
    `eq_target` non-empty, so `equalize` actually fires -- its failure must
    degrade to `equalize_warning` on the result, never fail the respawn."""
    registry = Registry(tmp_path / "registry.json")
    survivor = make_worker(
        tmp_path,
        name="ccw-x-review-survivor",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(survivor)
    _reach(registry, survivor, [WorkerState.READY])
    registry.mutate(
        survivor.name, lambda w: w.attach_pane(TMUX_PANE.format(1), "win-s")
    )
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])

    class BrokenEqualizeBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "pane_heights":
                self.calls.append(dict(request))
                raise BackendError("resize-pane failed")
            return super().run(request)

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": BrokenEqualizeBackend, "iterm2": OtherBackend},
    )
    RecordingBackend.world = {"orchestrator-pane", TMUX_PANE.format(1)}

    result = respawn(registry, target.name, parent_session_id="current-session")

    assert result["state"] == WorkerState.READY.value
    assert result["equalize_warning"] == "resize-pane failed"
    updated = registry.get(target.name)
    assert updated.state is WorkerState.READY
    assert updated.pane_ref is not None


def test_respawn_survives_a_convergence_resize_backend_error_and_reports_a_warning(
    tmp_path: Path, monkeypatch
) -> None:
    """An empty old root column means a horizontal split, which forwards the
    pinned active_pane_width=50 as a deferred follow-up resize -- its
    `BackendError` must degrade to `resize_warning` on the result, never
    fail the respawn (the pane split already succeeded)."""
    registry = Registry(tmp_path / "registry.json")
    target = make_worker(
        tmp_path,
        name="ccw-x-review-target",
        state=WorkerState.STARTING,
        pane_ref=None,
        parent_session_id="old-session",
    )
    registry.add(target)
    _reach(registry, target, [WorkerState.FAILED])

    class ResizeBlowsUpBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "resize":
                self.calls.append(dict(request))
                raise BackendError("tmux resize blew up")
            return super().run(request)

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": ResizeBlowsUpBackend, "iterm2": OtherBackend},
    )
    RecordingBackend.world = {"orchestrator-pane"}

    result = respawn(registry, target.name, parent_session_id="current-session")

    assert result["state"] == WorkerState.READY.value
    assert result["resize_warning"] == "tmux resize blew up"
    updated = registry.get(target.name)
    assert updated.state is WorkerState.READY
    assert updated.pane_ref is not None

from pathlib import Path
from typing import Any, ClassVar

import pytest
from _terminal_fakes import make_worker as _make_worker

import sukuna.infrastructure.terminal.operations as operations_module
import sukuna.usecase.resize as resize_module
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.domain.service.resize_convergence import MAX_RESIZE_ATTEMPTS
from sukuna.errors import NotFoundError, ValidationError
from sukuna.infrastructure.registry import Registry
from sukuna.usecase.resize import resize

TMUX_PANE = "%1"
ITERM2_PANE = "AAAAAAAA-0000-0000-0000-000000000001"


class _ResizeFakeBackend:
    instances: ClassVar[list["_ResizeFakeBackend"]] = []
    response: ClassVar[dict] = {"ok": True}
    # Default to "not active" so the self path's existing widen+select_pane
    # tests, written before the is_active gate existed, keep passing without
    # a stray `is_active_warning` polluting their exact-equality asserts --
    # a test that cares about the active-skip path overrides this per test.
    is_active_response: ClassVar[dict] = {"ok": True, "active": False}

    def __init__(self) -> None:
        self.calls: list[dict] = []
        type(self).instances.append(self)

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        if request["operation"] == "resize_context":
            # Wide, sibling-free window by default so `resize()`'s tmux
            # clamp orchestration never clamps -- most
            # of these tests only care about the final `resize`/warning
            # contract, not the clamp policy itself (covered by
            # tests/test_terminal_operations.py and
            # tests/test_pane_width_parity.py).
            return {"ok": True, "window_width": 1000, "other_pane_count": 0}
        if request["operation"] == "is_active":
            return dict(type(self).is_active_response)
        return dict(type(self).response)


class OtherBackend(_ResizeFakeBackend):
    instances: ClassVar[list[Any]] = []
    response: ClassVar[dict] = {"ok": True}
    is_active_response: ClassVar[dict] = {"ok": True, "active": False}


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch):
    _ResizeFakeBackend.instances = []
    _ResizeFakeBackend.response = {"ok": True}
    _ResizeFakeBackend.is_active_response = {"ok": True, "active": False}
    OtherBackend.instances = []
    OtherBackend.response = {"ok": True}
    OtherBackend.is_active_response = {"ok": True, "active": False}
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _ResizeFakeBackend, "iterm2": OtherBackend},
    )
    monkeypatch.setattr(resize_module, "load_active_pane_width", lambda: 55)
    monkeypatch.setattr(resize_module, "load_should_focus_worker", lambda: False)
    # The self-path spawn gate reads this from the real environment; clear it
    # so a test doesn't accidentally inherit a real orchestrator's session id.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    yield


def make_worker(
    repo: Path,
    *,
    pane_ref: str | None,
    suffix: str = "a",
    window_ref: str | None = None,
) -> WorkerRecord:
    return _make_worker(
        repo,
        suffix=suffix,
        pane_ref=pane_ref,
        window_ref=window_ref,
        parent_session_id="parent-1",
    )


def _mark_as_having_spawned(
    registry: Registry, tmp_path: Path, monkeypatch, session_id: str = "parent-1"
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    registry.add(make_worker(tmp_path, pane_ref=TMUX_PANE, suffix="spawned"))


_NOT_SUKUNA_RELATED_SKIP = {
    "ok": True,
    "skipped": (
        "this session is not recognized as sukuna-related (never spawned "
        "a worker, and its own pane is not a live sukuna worker record)"
    ),
}


def test_resize_targets_the_orchestrators_own_pane_when_no_worker_is_given(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE
    )

    result = resize(registry, None)

    assert result == {"ok": True}
    resize_call = next(
        call
        for instance in _ResizeFakeBackend.instances
        for call in instance.calls
        if call["operation"] == "resize"
    )
    assert resize_call["pane_ref"] == TMUX_PANE
    assert resize_call["percent"] == 55
    select_pane_call = next(
        call
        for instance in _ResizeFakeBackend.instances
        for call in instance.calls
        if call["operation"] == "select_pane"
    )
    assert select_pane_call["pane_ref"] == TMUX_PANE


def test_resize_self_path_calls_select_pane_for_iterm2_too(
    tmp_path: Path, monkeypatch
) -> None:
    """Self-focus must switch iTerm2's active session exactly as it
    already does for tmux."""
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "iterm2")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: ITERM2_PANE
    )

    result = resize(registry, None)

    assert result == {"ok": True}
    select_pane_call = next(
        call
        for instance in OtherBackend.instances
        for call in instance.calls
        if call["operation"] == "select_pane"
    )
    assert select_pane_call["pane_ref"] == ITERM2_PANE


def test_resize_fails_when_the_orchestrators_own_pane_cannot_be_resolved(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(resize_module, "orchestrator_pane_ref", lambda backend: None)

    with pytest.raises(ValidationError):
        resize(registry, None)

    assert _ResizeFakeBackend.instances == []


def test_resize_skips_the_self_path_when_the_session_has_never_spawned_a_worker(
    tmp_path: Path, monkeypatch
) -> None:
    """Gate 1 (spawn history) fails; gate 2 (own pane matches a live
    worker record) is still evaluated -- requiring `detect_backend()` --
    but the registry is empty, so it fails too."""
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE
    )

    result = resize(registry, None)

    assert result == _NOT_SUKUNA_RELATED_SKIP
    assert _ResizeFakeBackend.instances == []


def test_resize_skips_the_self_path_when_the_session_id_env_var_is_unset(
    tmp_path: Path, monkeypatch
) -> None:
    """A record with parent_session_id=None must not accidentally match an
    unset $CLAUDE_CODE_SESSION_ID via a `None == None` comparison. Gate 2
    is still evaluated: the manual worker's pane_ref matches, but its
    STARTING state (default from `WorkerRecord.create()`) is not one of
    `is_nested_caller_pane_state()`'s live states, so it still fails."""
    registry = Registry(tmp_path / "registry.json")
    manual_worker = WorkerRecord.create(
        name="ccw-x-review-manual",
        repo_root=str(tmp_path),
        worktree=str(tmp_path),
        parent_session_id=None,
    )
    manual_worker.pane_ref = TMUX_PANE
    registry.add(manual_worker)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE
    )

    result = resize(registry, None)

    assert result == _NOT_SUKUNA_RELATED_SKIP
    assert _ResizeFakeBackend.instances == []


def test_resize_skips_the_self_path_silently_in_a_bare_environment_when_never_spawned(
    tmp_path: Path, monkeypatch
) -> None:
    """With neither $TMUX nor $ITERM_SESSION_ID set, the real
    `detect_backend()` raises `ValidationError` -- gate 2's resolution
    must fail closed (own_pane_ref=None) instead of letting that
    propagate, preserving the existing "never-spawned, bare environment
    -> silent skip" contract."""
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)

    result = resize(registry, None)

    assert result == _NOT_SUKUNA_RELATED_SKIP
    assert _ResizeFakeBackend.instances == []


def test_resize_self_path_gate_stays_open_after_the_spawned_worker_is_closed(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    worker = registry.get("ccw-x-review-spawned")
    registry.transition(worker.name, WorkerState.READY)
    registry.transition(worker.name, WorkerState.BUSY)
    registry.transition(worker.name, WorkerState.REPORTED)
    registry.transition(worker.name, WorkerState.ACCEPTED)
    registry.transition(worker.name, WorkerState.CLOSED)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE
    )

    result = resize(registry, None)

    assert result == {"ok": True}
    resize_call = next(
        call
        for instance in _ResizeFakeBackend.instances
        for call in instance.calls
        if call["operation"] == "resize"
    )
    assert resize_call["pane_ref"] == TMUX_PANE


def test_resize_self_path_passes_gate_via_own_pane_matching_a_live_worker_record(
    tmp_path: Path, monkeypatch
) -> None:
    """No spawn history of its own (gate 1 fails), but its own pane_ref
    matches a live (BUSY) worker record in `pane_registry` -- a worker
    session self-focusing."""
    registry = Registry(tmp_path / "registry.json")
    pane_registry = Registry(tmp_path / "other-shard.json")
    worker = _make_worker(
        tmp_path, pane_ref=TMUX_PANE, worker_state=WorkerState.BUSY, suffix="live"
    )
    pane_registry.add(worker)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE
    )

    result = resize(registry, None, pane_registry=pane_registry)

    assert result == {"ok": True}
    resize_call = next(
        call
        for instance in _ResizeFakeBackend.instances
        for call in instance.calls
        if call["operation"] == "resize"
    )
    assert resize_call["pane_ref"] == TMUX_PANE


def test_resize_self_path_gate_2_ignores_a_stale_failed_records_pane_ref(
    tmp_path: Path, monkeypatch
) -> None:
    """A FAILED record's `pane_ref` must not satisfy gate 2 -- tmux
    reassigns pane ids after a server restart, so a stale FAILED record's
    `pane_ref` could otherwise collide with an unrelated live session."""
    registry = Registry(tmp_path / "registry.json")
    pane_registry = Registry(tmp_path / "other-shard.json")
    stale = _make_worker(
        tmp_path, pane_ref=TMUX_PANE, worker_state=WorkerState.FAILED, suffix="stale"
    )
    pane_registry.add(stale)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE
    )

    result = resize(registry, None, pane_registry=pane_registry)

    assert result == _NOT_SUKUNA_RELATED_SKIP
    assert _ResizeFakeBackend.instances == []


def test_resize_self_path_defaults_pane_registry_to_the_primary_registry(
    tmp_path: Path, monkeypatch
) -> None:
    """No `pane_registry` given -- gate 2 falls back to checking `registry`
    itself (single-file `--registry` overrides, and the existing
    `_dispatch_focus()` call site, both rely on this default)."""
    registry = Registry(tmp_path / "registry.json")
    worker = _make_worker(
        tmp_path, pane_ref=TMUX_PANE, worker_state=WorkerState.READY, suffix="live"
    )
    registry.add(worker)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE
    )

    result = resize(registry, None)

    assert result == {"ok": True}


def test_resize_self_path_skips_widen_and_select_pane_when_the_pane_is_already_active(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE
    )
    _ResizeFakeBackend.is_active_response = {"ok": True, "active": True}

    result = resize(registry, None)

    assert result == {
        "ok": True,
        "skipped": "the asking pane is already the active pane",
    }
    ops = [
        call["operation"]
        for instance in _ResizeFakeBackend.instances
        for call in instance.calls
    ]
    assert "resize" not in ops
    assert "select_pane" not in ops


def test_resize_self_path_still_widens_and_activates_when_is_active_backend_errors(
    tmp_path: Path, monkeypatch
) -> None:
    """`is_active` failing (BackendError) fails open: treated as "not
    active", so widen+select_pane still run, with a warning attached."""
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE
    )
    _ResizeFakeBackend.is_active_response = {"ok": False}  # -> BackendError

    result = resize(registry, None)

    assert result["ok"] is True
    assert "skipped" not in result
    assert "is_active_warning" in result
    ops = [
        call["operation"]
        for instance in _ResizeFakeBackend.instances
        for call in instance.calls
    ]
    assert "resize" in ops
    assert "select_pane" in ops


def test_resize_self_path_skips_widen_and_select_pane_when_the_iterm2_pane_is_already_active(
    tmp_path: Path, monkeypatch
) -> None:
    """Backend parity with the tmux case above: the active-pane gate is
    backend-agnostic (`terminal_ops.is_active()` dispatches by `backend`
    the same way every other op in `resize()` does)."""
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "iterm2")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: ITERM2_PANE
    )
    OtherBackend.is_active_response = {"ok": True, "active": True}

    result = resize(registry, None)

    assert result == {
        "ok": True,
        "skipped": "the asking pane is already the active pane",
    }
    ops = [
        call["operation"]
        for instance in OtherBackend.instances
        for call in instance.calls
    ]
    assert "resize" not in ops
    assert "select_pane" not in ops


def test_resize_self_path_still_widens_and_activates_when_iterm2_is_active_backend_errors(
    tmp_path: Path, monkeypatch
) -> None:
    """Backend parity with the tmux case above: a `BackendError` from
    `is_active` fails open on iTerm2 too."""
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "iterm2")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: ITERM2_PANE
    )
    OtherBackend.is_active_response = {"ok": False}  # -> BackendError

    result = resize(registry, None)

    assert result["ok"] is True
    assert "skipped" not in result
    assert "is_active_warning" in result
    ops = [
        call["operation"]
        for instance in OtherBackend.instances
        for call in instance.calls
    ]
    assert "resize" in ops
    assert "select_pane" in ops


def test_resize_named_worker_path_never_calls_is_active(tmp_path: Path) -> None:
    """The is_active gate is scoped to the self path only -- the
    named-worker path (`--worker X`, and `_dispatch_focus()`'s BUSY
    follow) is unaffected by this card."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE)
    registry.add(worker)
    _ResizeFakeBackend.is_active_response = {"ok": True, "active": True}

    result = resize(registry, worker.name)

    assert result == {"ok": True}
    ops = [
        call["operation"]
        for instance in _ResizeFakeBackend.instances
        for call in instance.calls
    ]
    assert "is_active" not in ops


def test_resize_targets_a_named_workers_pane(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE)
    registry.add(worker)

    result = resize(registry, worker.name)

    assert result == {"ok": True}
    call = _ResizeFakeBackend.instances[-1].calls[-1]
    assert call["pane_ref"] == TMUX_PANE
    assert call["percent"] == 55


def test_resize_infers_the_iterm2_backend_from_the_workers_pane_shape(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=ITERM2_PANE)
    registry.add(worker)

    resize(registry, worker.name)

    assert OtherBackend.instances[-1].calls[-1]["pane_ref"] == ITERM2_PANE
    assert _ResizeFakeBackend.instances == []


def test_resize_rejects_a_worker_with_no_pane(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=None)
    registry.add(worker)

    with pytest.raises(ValidationError):
        resize(registry, worker.name)

    assert _ResizeFakeBackend.instances == []


def test_resize_rejects_an_unknown_worker_name(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")

    with pytest.raises(NotFoundError):
        resize(registry, "ccw-x-review-does-not-exist")


def test_resize_forwards_a_resize_warning_from_the_backend(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE)
    registry.add(worker)
    _ResizeFakeBackend.response = {"ok": True, "resize_warning": "clamped to 42%"}

    result = resize(registry, worker.name)

    assert result["resize_warning"] == "clamped to 42%"


# -- window frame save/restore --


class FramingBackend(_ResizeFakeBackend):
    instances: ClassVar[list[Any]] = []
    frame: ClassVar[dict] = {
        "ok": True,
        "x": 5.0,
        "y": 6.0,
        "width": 111.0,
        "height": 222.0,
    }
    resolved_window_ref: ClassVar[dict] = {"ok": True, "window_ref": "resolved-win"}

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        if request["operation"] == "get_window_frame":
            return dict(type(self).frame)
        if request["operation"] == "resolve_window_ref":
            return dict(type(self).resolved_window_ref)
        if request["operation"] == "is_active":
            return dict(type(self).is_active_response)
        return dict(type(self).response)


def test_resize_saves_and_restores_the_window_frame_for_a_named_iterm2_worker(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=ITERM2_PANE, window_ref="win-1")
    registry.add(worker)
    FramingBackend.instances = []  # not covered by the autouse `_fake_backends` reset
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _ResizeFakeBackend, "iterm2": FramingBackend},
    )

    result = resize(registry, worker.name)

    assert result == {"ok": True}
    calls = [call for instance in FramingBackend.instances for call in instance.calls]
    ops = [call["operation"] for call in calls]
    assert (
        ops.index("get_window_frame")
        < ops.index("resize")
        < ops.index("set_window_frame")
    )
    set_call = calls[ops.index("set_window_frame")]
    assert set_call == {
        "operation": "set_window_frame",
        "window_ref": "win-1",
        "x": 5.0,
        "y": 6.0,
        "width": 111.0,
        "height": 222.0,
    }


def test_resize_survives_a_window_frame_read_failure_without_failing_the_resize(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=ITERM2_PANE, window_ref="win-1")
    registry.add(worker)

    class NoFrameBackend(_ResizeFakeBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            self.calls.append(dict(request))
            if request["operation"] == "get_window_frame":
                return {"ok": True}  # missing x/y/width/height -> BackendError
            return dict(type(self).response)

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _ResizeFakeBackend, "iterm2": NoFrameBackend},
    )

    result = resize(registry, worker.name)

    assert result == {"ok": True}
    ops = [
        call["operation"]
        for instance in NoFrameBackend.instances
        for call in instance.calls
    ]
    assert "set_window_frame" not in ops


def test_resize_tmux_path_never_calls_window_frame_ops_even_when_window_ref_is_set(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE, window_ref="win-1")
    registry.add(worker)

    resize(registry, worker.name)

    ops = [
        call["operation"]
        for instance in _ResizeFakeBackend.instances
        for call in instance.calls
    ]
    assert "get_window_frame" not in ops
    assert "set_window_frame" not in ops


def test_resize_self_path_resolves_window_ref_live_and_saves_restores_frame_for_iterm2(
    tmp_path: Path, monkeypatch
) -> None:
    """The self-resize path has no `WorkerRecord` to draw a `window_ref`
    from, so it must resolve one live from `pane_ref` instead of silently
    skipping frame save/restore."""
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "iterm2")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: ITERM2_PANE
    )
    FramingBackend.instances = []  # not covered by the autouse `_fake_backends` reset
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _ResizeFakeBackend, "iterm2": FramingBackend},
    )

    result = resize(registry, None)

    assert result == {"ok": True}
    calls = [call for instance in FramingBackend.instances for call in instance.calls]
    ops = [call["operation"] for call in calls]
    assert (
        ops.index("resolve_window_ref")
        < ops.index("get_window_frame")
        < ops.index("set_window_frame")
    )
    assert calls[ops.index("resolve_window_ref")] == {
        "operation": "resolve_window_ref",
        "pane_ref": ITERM2_PANE,
    }
    set_call = calls[ops.index("set_window_frame")]
    assert set_call["window_ref"] == "resolved-win"


def test_resize_self_path_survives_a_window_ref_resolution_failure_without_failing_the_resize(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "iterm2")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: ITERM2_PANE
    )

    class NoResolveBackend(_ResizeFakeBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            self.calls.append(dict(request))
            if request["operation"] == "resolve_window_ref":
                return {
                    "ok": False
                }  # fails `ResolveWindowRefResponse`'s `ok: Literal[True]` -> BackendError
            if request["operation"] == "is_active":
                return dict(type(self).is_active_response)
            return dict(type(self).response)

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _ResizeFakeBackend, "iterm2": NoResolveBackend},
    )

    result = resize(registry, None)

    assert result == {"ok": True}


def test_resize_self_path_skips_window_ref_resolution_for_tmux(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE
    )

    result = resize(registry, None)

    assert result == {"ok": True}
    ops = [
        call["operation"]
        for instance in _ResizeFakeBackend.instances
        for call in instance.calls
    ]
    assert "resolve_window_ref" not in ops
    assert "get_window_frame" not in ops
    assert "set_window_frame" not in ops


def test_resize_saves_and_restores_the_window_frame_for_a_stacked_column_member(
    tmp_path: Path, monkeypatch
) -> None:
    """The window frame save/restore try/finally span in `resize()` above
    wraps the *entire* `terminal_ops.resize()`/`converge_active_pane_width()`
    call as one opaque unit -- it never inspects pane tree shape.
    Nested-Splitter support (per_session write, root-level target
    resolution) lives entirely inside `iterm_script.py`'s `resize()`, one
    level below this dispatch; from here, a stacked-column member is just
    another `pane_ref`. This test pins that: even when the target names a
    (would-be, in production) nested-sibling column member, this
    try/finally still wraps the resize call and still fires
    get_window_frame -> resize -> set_window_frame in order, exactly like
    the flat-sibling case above."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=ITERM2_PANE, window_ref="win-1")
    registry.add(worker)
    FramingBackend.instances = []  # not covered by the autouse `_fake_backends` reset
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _ResizeFakeBackend, "iterm2": FramingBackend},
    )

    result = resize(registry, worker.name)

    assert result == {"ok": True}
    calls = [call for instance in FramingBackend.instances for call in instance.calls]
    ops = [call["operation"] for call in calls]
    assert (
        ops.index("get_window_frame")
        < ops.index("resize")
        < ops.index("set_window_frame")
    )


# -- active_pane_width convergence retry loop (card, "fixresizeconverge"):
# `resize()` shares usecase/pane_convergence.py's bounded retry with
# usecase/spawn.py -- see tests/test_pane_convergence.py for the loop's own
# unit tests (tolerance, overshoot correction, max-attempts). These only
# check that `resize()` actually drives the shared loop instead of a single
# unretried call, and that it reports the loop's own warning the same way
# spawn does. -


class ConvergingBackend(_ResizeFakeBackend):
    """iterm2-registered fake that reports a scripted `achieved_percent`
    sequence across successive `resize` calls, like FramingBackend in
    tests/test_usecase_spawn.py."""

    instances: ClassVar[list[Any]] = []
    achieved_sequence: ClassVar[list[int]] = []

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        if request["operation"] == "resize":
            index = (
                sum(
                    1
                    for instance in type(self).instances
                    for call in instance.calls
                    if call["operation"] == "resize"
                )
                - 1
            )
            return {"ok": True, "achieved_percent": type(self).achieved_sequence[index]}
        if request["operation"] == "is_active":
            return dict(type(self).is_active_response)
        return dict(type(self).response)


def test_resize_retries_a_named_iterm2_worker_until_within_tolerance(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=ITERM2_PANE)
    registry.add(worker)
    ConvergingBackend.instances = []
    ConvergingBackend.achieved_sequence = [40, 50, 56]  # target is 55 (fixture default)
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _ResizeFakeBackend, "iterm2": ConvergingBackend},
    )

    result = resize(registry, worker.name)

    assert result == {"ok": True, "achieved_percent": 56}
    resize_calls = [
        call
        for instance in ConvergingBackend.instances
        for call in instance.calls
        if call["operation"] == "resize"
    ]
    assert len(resize_calls) == 3


def test_resize_reports_a_warning_when_the_convergence_loop_never_reaches_tolerance(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=ITERM2_PANE)
    registry.add(worker)
    ConvergingBackend.instances = []
    ConvergingBackend.achieved_sequence = [
        10
    ] * MAX_RESIZE_ATTEMPTS  # never converges toward 55
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _ResizeFakeBackend, "iterm2": ConvergingBackend},
    )

    result = resize(registry, worker.name)

    assert "55%" in result["resize_warning"]
    assert "10%" in result["resize_warning"]
    resize_calls = [
        call
        for instance in ConvergingBackend.instances
        for call in instance.calls
        if call["operation"] == "resize"
    ]
    assert len(resize_calls) == MAX_RESIZE_ATTEMPTS


def test_resize_self_path_also_drives_the_convergence_loop(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "iterm2")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: ITERM2_PANE
    )
    ConvergingBackend.instances = []
    ConvergingBackend.achieved_sequence = [40, 55]
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _ResizeFakeBackend, "iterm2": ConvergingBackend},
    )

    result = resize(registry, None)

    assert result == {"ok": True, "achieved_percent": 55}
    resize_calls = [
        call
        for instance in ConvergingBackend.instances
        for call in instance.calls
        if call["operation"] == "resize"
    ]
    assert len(resize_calls) == 2


# -- select-pane on focus --


def test_resize_worker_path_does_not_call_select_pane_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE)
    registry.add(worker)
    monkeypatch.setattr(resize_module, "load_should_focus_worker", lambda: False)

    result = resize(registry, worker.name)

    assert result == {"ok": True}
    ops = [
        call["operation"]
        for instance in _ResizeFakeBackend.instances
        for call in instance.calls
    ]
    assert "select_pane" not in ops


def test_resize_worker_path_calls_select_pane_when_should_focus_worker_is_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE)
    registry.add(worker)
    monkeypatch.setattr(resize_module, "load_should_focus_worker", lambda: True)

    result = resize(registry, worker.name)

    assert result == {"ok": True}
    select_pane_call = next(
        call
        for instance in _ResizeFakeBackend.instances
        for call in instance.calls
        if call["operation"] == "select_pane"
    )
    assert select_pane_call["pane_ref"] == TMUX_PANE


def test_resize_reports_a_select_pane_warning_without_failing_the_resize(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    _mark_as_having_spawned(registry, tmp_path, monkeypatch)
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE
    )

    class SelectPaneFailingBackend(_ResizeFakeBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            self.calls.append(dict(request))
            if request["operation"] == "select_pane":
                return {
                    "ok": False
                }  # fails `SelectPaneResponse`'s `ok: Literal[True]` -> BackendError
            if request["operation"] == "resize_context":
                return {"ok": True, "window_width": 1000, "other_pane_count": 0}
            if request["operation"] == "is_active":
                return dict(type(self).is_active_response)
            return dict(type(self).response)

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {
            "tmux": SelectPaneFailingBackend,
            "iterm2": OtherBackend,
        },
    )

    result = resize(registry, None)

    assert result["ok"] is True
    assert "select_pane_warning" in result


def test_resize_select_pane_warning_coexists_with_a_resize_warning(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE)
    registry.add(worker)
    monkeypatch.setattr(resize_module, "load_should_focus_worker", lambda: True)

    class MixedWarningBackend(_ResizeFakeBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            self.calls.append(dict(request))
            if request["operation"] == "select_pane":
                return {"ok": False}  # -> BackendError, becomes select_pane_warning
            if request["operation"] == "resize":
                return {"ok": True, "resize_warning": "clamped to 42%"}
            if request["operation"] == "resize_context":
                return {"ok": True, "window_width": 1000, "other_pane_count": 0}
            return dict(type(self).response)

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": MixedWarningBackend, "iterm2": OtherBackend},
    )

    result = resize(registry, worker.name)

    assert result["resize_warning"] == "clamped to 42%"
    assert "select_pane_warning" in result

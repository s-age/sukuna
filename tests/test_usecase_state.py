"""`state()` dispatches an automatic focus-follow to the deepest BUSY
worker (or falls back to the caller's own pane) right after a
`busy`/`reported` transition commits."""

from pathlib import Path
from typing import ClassVar, TypedDict, Unpack

import pytest
from _terminal_fakes import make_worker as _make_worker

import sukuna.infrastructure.terminal.operations as operations_module
import sukuna.usecase.resize as resize_module
import sukuna.usecase.state as state_module
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.errors import BackendError, ConflictError, ValidationError
from sukuna.infrastructure.registry import Registry
from sukuna.usecase.state import state

TMUX_PANE_CALLER = "%1"


class _StateFakeBackend:
    instances: ClassVar[list["_StateFakeBackend"]] = []
    verify_responses: ClassVar[dict[str, dict]] = {}
    verify_raises: ClassVar[set[str]] = set()

    def __init__(self) -> None:
        self.calls: list[dict] = []
        type(self).instances.append(self)

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        if request["operation"] == "resize_context":
            # Wide, sibling-free window by default so `resize()`'s tmux
            # clamp orchestration never clamps -- these
            # tests only care whether a resize was dispatched to the right
            # pane, not about the clamp policy itself.
            return {"ok": True, "window_width": 1000, "other_pane_count": 0}
        if request["operation"] == "verify":
            pane_ref = request["pane_ref"]
            if pane_ref in type(self).verify_raises:
                raise BackendError("verify failed")
            return type(self).verify_responses.get(
                pane_ref, {"ok": True, "exists": False}
            )
        return {"ok": True}


class TmuxRecordingBackend(_StateFakeBackend):
    instances: ClassVar[list["_StateFakeBackend"]] = []
    verify_responses: ClassVar[dict[str, dict]] = {}
    verify_raises: ClassVar[set[str]] = set()


class ItermRecordingBackend(_StateFakeBackend):
    instances: ClassVar[list["_StateFakeBackend"]] = []
    verify_responses: ClassVar[dict[str, dict]] = {}
    verify_raises: ClassVar[set[str]] = set()


def _unreachable_backend() -> str:
    raise AssertionError("backend detection should not have been reached")


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch):
    for backend in (_StateFakeBackend, TmuxRecordingBackend, ItermRecordingBackend):
        backend.instances = []
        backend.verify_responses = {}
        backend.verify_raises = set()
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": TmuxRecordingBackend, "iterm2": ItermRecordingBackend},
    )
    # `resize()` reads both live from the settings file (`sukuna.usecase.resize`'s
    # own module namespace, per commit 5745a61 and the same "from X import Y
    # binding" trap it fixed) -- patch both here so these tests never depend
    # on the developer machine's real `sukuna-cli init` choices.
    monkeypatch.setattr(resize_module, "load_should_focus_worker", lambda: False)
    monkeypatch.setattr(resize_module, "load_active_pane_width", lambda: 50)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    yield


class _WorkerOverrides(TypedDict, total=False):
    worker_state: WorkerState
    pane_ref: str | None
    parent_session_id: str | None
    parent_worker_name: str | None


def make_worker(
    repo: Path, *, name: str, **overrides: Unpack[_WorkerOverrides]
) -> WorkerRecord:
    overrides.setdefault("worker_state", WorkerState.READY)
    return _make_worker(repo, name=name, **overrides)


def _resize_calls(operation: str) -> list[dict]:
    return [
        call
        for instance in TmuxRecordingBackend.instances
        for call in instance.calls
        if call["operation"] == operation
    ]


def test_busy_transition_focuses_the_deepest_busy_worker_even_when_a_shallower_one_just_transitioned(
    tmp_path: Path, monkeypatch
) -> None:
    """Pins the card's explicit scenario: dispatching `child-a` to BUSY
    must not steal focus from a deeper, longer-running BUSY grandchild --
    depth is the primary key, `updated_at` only a tie-break (open decision 5)."""
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "orch-1")
    monkeypatch.setattr(state_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        state_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE_CALLER
    )

    child_a = make_worker(
        tmp_path,
        name="ccw-x-a",
        worker_state=WorkerState.READY,
        pane_ref="%2",
        parent_session_id="orch-1",
    )
    grandchild_b = make_worker(
        tmp_path,
        name="ccw-x-b",
        worker_state=WorkerState.BUSY,
        pane_ref="%3",
        parent_worker_name="ccw-x-a",
    )
    registry.add(child_a)
    registry.add(grandchild_b)

    result = state(registry, "ccw-x-a", "busy")

    assert result["state"] == "busy"
    assert "focus_warning" not in result
    resize_calls = _resize_calls("resize")
    assert len(resize_calls) == 1
    assert resize_calls[0]["pane_ref"] == "%3"
    # Named-worker path: select_pane is gated by should_focus_worker (False here).
    assert _resize_calls("select_pane") == []


def test_reported_transition_falls_back_to_the_callers_own_pane_when_no_busy_worker_remains(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "orch-1")
    monkeypatch.setattr(state_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        state_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE_CALLER
    )
    # resize()'s own self path resolves the backend/pane again, independently
    # of state.py's scope check -- both bound names live in different module
    # namespaces and must both be patched.
    monkeypatch.setattr(resize_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        resize_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE_CALLER
    )

    worker = make_worker(
        tmp_path,
        name="ccw-x-a",
        worker_state=WorkerState.BUSY,
        pane_ref="%2",
        parent_session_id="orch-1",
    )
    registry.add(worker)

    result = state(registry, "ccw-x-a", "reported")

    assert result["state"] == "reported"
    assert "focus_warning" not in result
    resize_calls = _resize_calls("resize")
    assert len(resize_calls) == 1
    assert resize_calls[0]["pane_ref"] == TMUX_PANE_CALLER
    select_pane_calls = _resize_calls("select_pane")
    assert len(select_pane_calls) == 1
    assert select_pane_calls[0]["pane_ref"] == TMUX_PANE_CALLER


def test_no_dispatch_when_the_caller_itself_is_a_pane_holding_worker(
    tmp_path: Path, monkeypatch
) -> None:
    """A worker with a live grandchild is out of v1 scope ("scope
    determination" landing shape): the caller's own pane matching a
    pane-holding worker record means the caller is nested, and the
    mechanism no-ops entirely."""
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setattr(state_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(state_module, "orchestrator_pane_ref", lambda backend: "%5")

    caller_worker = make_worker(
        tmp_path, name="ccw-x-caller", worker_state=WorkerState.READY, pane_ref="%5"
    )
    target = make_worker(
        tmp_path, name="ccw-x-target", worker_state=WorkerState.READY, pane_ref="%6"
    )
    registry.add(caller_worker)
    registry.add(target)

    result = state(registry, "ccw-x-target", "busy")

    assert result["state"] == "busy"
    assert "focus_warning" not in result
    assert TmuxRecordingBackend.instances == []


def test_accepted_transition_does_not_trigger_focus_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setattr(state_module, "detect_backend", _unreachable_backend)
    worker = make_worker(
        tmp_path, name="ccw-x-a", worker_state=WorkerState.REPORTED, pane_ref="%2"
    )
    registry.add(worker)

    result = state(registry, "ccw-x-a", "accepted")

    assert result["state"] == "accepted"
    assert "focus_warning" not in result
    assert TmuxRecordingBackend.instances == []


def test_focus_dispatch_is_silently_skipped_when_backend_detection_fails(
    tmp_path: Path, monkeypatch
) -> None:
    def _raise_validation() -> str:
        raise ValidationError("no supported terminal detected")

    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setattr(state_module, "detect_backend", _raise_validation)
    worker = make_worker(
        tmp_path, name="ccw-x-a", worker_state=WorkerState.READY, pane_ref="%2"
    )
    registry.add(worker)

    result = state(registry, "ccw-x-a", "busy")

    assert result["state"] == "busy"
    assert "focus_warning" not in result
    assert TmuxRecordingBackend.instances == []


def test_state_rejects_a_direct_transition_to_closed(
    tmp_path: Path, monkeypatch
) -> None:
    """`close()` is the sole CLOSED entry point -- it owns pane
    destruction, the live-child guard, and equalize. `state` must reject
    "closed" before touching the registry, so the record stays ACCEPTED
    and the pane_ref survives untouched, and no backend/focus dispatch is
    ever reached."""
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setattr(state_module, "detect_backend", _unreachable_backend)
    worker = make_worker(
        tmp_path,
        name="ccw-x-a",
        worker_state=WorkerState.ACCEPTED,
        pane_ref="%2",
    )
    registry.add(worker)

    with pytest.raises(ValidationError, match="sukuna close"):
        state(registry, "ccw-x-a", "closed")

    stored = registry.get("ccw-x-a")
    assert stored.state is WorkerState.ACCEPTED
    assert stored.pane_ref == "%2"
    assert TmuxRecordingBackend.instances == []


@pytest.mark.parametrize(
    "worker_state",
    [WorkerState.CLOSED, WorkerState.FAILED, WorkerState.TIMED_OUT],
)
def test_state_rejects_a_manual_transition_to_starting(
    tmp_path: Path, monkeypatch, worker_state: WorkerState
) -> None:
    """Without an explicit guard here, a manual `sukuna state --state
    starting` on a pane-less terminal worker would bypass every one of
    `respawn()`'s safety checks (worktree existence, backend-mismatch,
    actual pane creation) and leave a STARTING record with no pane --
    symmetric with the CLOSED guard above."""
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setattr(state_module, "detect_backend", _unreachable_backend)
    worker = make_worker(
        tmp_path,
        name="ccw-x-a",
        worker_state=worker_state,
        pane_ref=None,
    )
    registry.add(worker)

    with pytest.raises(ValidationError, match="sukuna respawn"):
        state(registry, "ccw-x-a", "starting")

    stored = registry.get("ccw-x-a")
    assert stored.state is worker_state
    assert stored.pane_ref is None
    assert TmuxRecordingBackend.instances == []


def test_state_still_allows_a_direct_transition_to_failed(
    tmp_path: Path, monkeypatch
) -> None:
    """The card's carve-out: `failed` stays reachable via `state` as a
    manual recovery path for records stuck without a live pane -- it does
    not destroy a pane, so it is not a `close()` bypass."""
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setattr(state_module, "detect_backend", _unreachable_backend)
    worker = make_worker(
        tmp_path,
        name="ccw-x-a",
        worker_state=WorkerState.ACCEPTED,
        pane_ref=None,
    )
    registry.add(worker)

    result = state(registry, "ccw-x-a", "failed")

    assert result["state"] == "failed"
    stored = registry.get("ccw-x-a")
    assert stored.state is WorkerState.FAILED
    # pane_ref=None must short-circuit the guard without ever reaching
    # the backend for a verify call.
    assert TmuxRecordingBackend.instances == []
    assert ItermRecordingBackend.instances == []


def test_state_allows_a_manual_transition_to_failed_when_the_pane_verifies_gone(
    tmp_path: Path,
) -> None:
    """Once `verify` confirms the pane is actually gone, a manual FAILED
    transition proceeds (and the guard does not itself detach the
    now-stale pane_ref; that remains reconcile's job)."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, name="ccw-x-a", worker_state=WorkerState.READY, pane_ref="%2"
    )
    registry.add(worker)
    TmuxRecordingBackend.verify_responses["%2"] = {"ok": True, "exists": False}

    result = state(registry, "ccw-x-a", "failed")

    assert result["state"] == "failed"
    stored = registry.get("ccw-x-a")
    assert stored.state is WorkerState.FAILED
    assert stored.pane_ref == "%2"


def test_state_rejects_a_manual_transition_to_failed_when_the_pane_still_exists(
    tmp_path: Path,
) -> None:
    """A manual FAILED against a worker whose pane verifies as still alive
    is a contract violation --
    reject it, leaving state and pane_ref untouched, instead of letting the
    record go FAILED with a live pane_ref (the condition that later fools
    `_dispatch_focus()`'s `is_nested_caller` check)."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, name="ccw-x-a", worker_state=WorkerState.READY, pane_ref="%2"
    )
    registry.add(worker)
    TmuxRecordingBackend.verify_responses["%2"] = {"ok": True, "exists": True}

    with pytest.raises(ValidationError, match="ccw-x-a"):
        state(registry, "ccw-x-a", "failed")

    stored = registry.get("ccw-x-a")
    assert stored.state is WorkerState.READY
    assert stored.pane_ref == "%2"


def test_state_rejects_a_manual_transition_to_failed_when_verify_itself_fails(
    tmp_path: Path,
) -> None:
    """When pane-liveness verification itself fails (backend unreachable),
    deny the manual FAILED transition on the safe side rather than
    letting it through."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, name="ccw-x-a", worker_state=WorkerState.READY, pane_ref="%2"
    )
    registry.add(worker)
    TmuxRecordingBackend.verify_raises.add("%2")

    with pytest.raises(ValidationError, match="ccw-x-a"):
        state(registry, "ccw-x-a", "failed")

    stored = registry.get("ccw-x-a")
    assert stored.state is WorkerState.READY
    assert stored.pane_ref == "%2"


def test_state_rejects_a_manual_transition_to_failed_when_the_record_changes_after_the_guard(
    tmp_path: Path, monkeypatch
) -> None:
    """The guard's read/verify
    and the transition are separate registry accesses with a backend
    round-trip in between. A concurrent writer landing in that window
    (typically a respawn re-attaching a live pane) must surface as
    `ConflictError` — carried by the guarded snapshot's `updated_at` as a
    CAS — instead of the transition silently applying to the changed record
    and recreating the live-pane_ref-FAILED state the guard exists to
    prevent. The concurrent write is injected from inside the (patched)
    verify call, exactly where the real window sits."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, name="ccw-x-a", worker_state=WorkerState.READY, pane_ref="%2"
    )
    registry.add(worker)

    def verify_gone_with_concurrent_write(pane_ref: str) -> bool:
        registry.transition("ccw-x-a", WorkerState.BUSY)
        return False

    monkeypatch.setattr(
        state_module, "verify_pane_exists", verify_gone_with_concurrent_write
    )

    with pytest.raises(ConflictError, match="modified concurrently"):
        state(registry, "ccw-x-a", "failed")

    stored = registry.get("ccw-x-a")
    assert stored.state is WorkerState.BUSY
    assert stored.pane_ref == "%2"


def test_focus_warning_surfaces_when_resize_fails_after_a_target_is_chosen(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "orch-1")
    monkeypatch.setattr(state_module, "detect_backend", lambda: "tmux")
    monkeypatch.setattr(
        state_module, "orchestrator_pane_ref", lambda backend: TMUX_PANE_CALLER
    )

    def _boom(registry, name):
        raise BackendError("could not resize pane")

    monkeypatch.setattr(state_module, "resize", _boom)

    worker = make_worker(
        tmp_path,
        name="ccw-x-a",
        worker_state=WorkerState.READY,
        pane_ref="%2",
        parent_session_id="orch-1",
    )
    registry.add(worker)

    result = state(registry, "ccw-x-a", "busy")

    assert result["state"] == "busy"
    assert result["focus_warning"] == "could not resize pane"


def test_state_allows_a_direct_transition_to_timed_out_without_a_pane(
    tmp_path: Path, monkeypatch
) -> None:
    """`pane_ref is None` must short-circuit the warn check without ever
    reaching the backend for a verify call, mirroring FAILED's own
    short-circuit."""
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setattr(state_module, "detect_backend", _unreachable_backend)
    worker = make_worker(
        tmp_path,
        name="ccw-x-a",
        worker_state=WorkerState.READY,
        pane_ref=None,
    )
    registry.add(worker)

    result = state(registry, "ccw-x-a", "timed_out")

    assert result["state"] == "timed_out"
    assert "pane_alive_warning" not in result
    stored = registry.get("ccw-x-a")
    assert stored.state is WorkerState.TIMED_OUT
    assert TmuxRecordingBackend.instances == []
    assert ItermRecordingBackend.instances == []


def test_state_warns_on_a_manual_transition_to_timed_out_when_the_pane_still_exists(
    tmp_path: Path,
) -> None:
    """A manual TIMED_OUT against a worker whose pane verifies as still
    alive is allowed to proceed -- the transition is never rejected --
    but the result carries `pane_alive_warning` to surface the
    false-positive-`is_nested_caller` risk that FAILED's guard rejects
    outright but TIMED_OUT leaves unfixed by design."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, name="ccw-x-a", worker_state=WorkerState.READY, pane_ref="%2"
    )
    registry.add(worker)
    TmuxRecordingBackend.verify_responses["%2"] = {"ok": True, "exists": True}

    result = state(registry, "ccw-x-a", "timed_out")

    assert result["state"] == "timed_out"
    assert "ccw-x-a" in result["pane_alive_warning"]
    assert "%2" in result["pane_alive_warning"]
    stored = registry.get("ccw-x-a")
    assert stored.state is WorkerState.TIMED_OUT
    assert stored.pane_ref == "%2"


def test_state_does_not_warn_on_a_manual_transition_to_timed_out_when_the_pane_verifies_gone(
    tmp_path: Path,
) -> None:
    """The transition proceeds silently when a live `pane_ref` verifies as
    already gone -- nothing worth warning about."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, name="ccw-x-a", worker_state=WorkerState.READY, pane_ref="%2"
    )
    registry.add(worker)
    TmuxRecordingBackend.verify_responses["%2"] = {"ok": True, "exists": False}

    result = state(registry, "ccw-x-a", "timed_out")

    assert result["state"] == "timed_out"
    assert "pane_alive_warning" not in result
    stored = registry.get("ccw-x-a")
    assert stored.state is WorkerState.TIMED_OUT
    assert stored.pane_ref == "%2"


def test_state_does_not_warn_on_a_manual_transition_to_timed_out_when_verify_itself_fails(
    tmp_path: Path,
) -> None:
    """When pane-liveness verification itself fails (backend unreachable),
    stay quiet rather than warn -- the inverse safe side from FAILED's,
    because TIMED_OUT has no transition left to deny."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, name="ccw-x-a", worker_state=WorkerState.READY, pane_ref="%2"
    )
    registry.add(worker)
    TmuxRecordingBackend.verify_raises.add("%2")

    result = state(registry, "ccw-x-a", "timed_out")

    assert result["state"] == "timed_out"
    assert "pane_alive_warning" not in result
    stored = registry.get("ccw-x-a")
    assert stored.state is WorkerState.TIMED_OUT
    assert stored.pane_ref == "%2"

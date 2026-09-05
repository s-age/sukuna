from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import pytest
from _terminal_fakes import ITERM2_PANE, TMUX_PANE
from _terminal_fakes import make_worker as _make_worker

import sukuna.infrastructure.terminal.operations as operations_module
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.errors import BackendError, ConflictError, RegistryIOError, ValidationError
from sukuna.infrastructure.registry import Registry
from sukuna.usecase.close import close
from sukuna.usecase.reconcile import reconcile


class _CloseFakeBackend:
    instances: ClassVar[list["_CloseFakeBackend"]] = []
    verify_responses: ClassVar[dict[str, dict]] = {}

    def __init__(self) -> None:
        self.calls: list[dict] = []
        type(self).instances.append(self)

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        if request["operation"] == "verify":
            return type(self).verify_responses[request["pane_ref"]]
        if request["operation"] == "pane_heights":
            # tmux equalize reads the column's heights first; an
            # already-equal column keeps the follow-up writes trivial.
            return {"ok": True, "heights": [24] * len(request["column_pane_refs"])}
        return {"ok": True}


class OtherBackend(_CloseFakeBackend):
    instances: ClassVar[list["_CloseFakeBackend"]] = []
    verify_responses: ClassVar[dict[str, dict]] = {}


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch):
    _CloseFakeBackend.instances = []
    _CloseFakeBackend.verify_responses = {}
    OtherBackend.instances = []
    OtherBackend.verify_responses = {}
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _CloseFakeBackend, "iterm2": OtherBackend},
    )
    yield


def make_worker(
    repo: Path,
    *,
    pane_ref: str | None,
    suffix: str,
    parent_worker_name: str | None = None,
    window_ref: str | None = None,
) -> WorkerRecord:
    return _make_worker(
        repo,
        suffix=suffix,
        pane_ref=pane_ref,
        window_ref=window_ref,
        parent_session_id="parent-1",
        parent_worker_name=parent_worker_name,
    )


def test_close_dispatches_by_the_pane_refs_own_shape(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    for state in (
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
    ):
        registry.transition(worker.name, state)

    result = close(registry, worker.name)

    assert result["state"] == WorkerState.CLOSED.value
    assert _CloseFakeBackend.instances[-1].calls[-1] == {
        "operation": "close",
        "pane_ref": TMUX_PANE.format(1),
    }
    assert OtherBackend.instances == []


def test_close_clears_the_pane_ref_on_the_closed_record(tmp_path: Path) -> None:
    """The success path must clear pane_ref/window_ref on the same write
    as the CLOSED transition -- otherwise the CLOSED record keeps
    pointing at an already-destroyed pane."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a", window_ref="@1"
    )
    registry.add(worker)
    _accept(registry, worker)

    result = close(registry, worker.name)

    assert result["state"] == WorkerState.CLOSED.value
    assert result["pane_ref"] is None
    assert result["window_ref"] is None
    current = registry.get(worker.name)
    assert current.pane_ref is None
    assert current.window_ref is None


def test_close_routes_an_iterm2_shaped_pane_to_the_iterm2_backend(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=ITERM2_PANE.format(1), suffix="a")
    registry.add(worker)
    for state in (
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
    ):
        registry.transition(worker.name, state)

    result = close(registry, worker.name)

    assert result["state"] == WorkerState.CLOSED.value
    assert OtherBackend.instances[-1].calls[-1] == {
        "operation": "close",
        "pane_ref": ITERM2_PANE.format(1),
    }
    assert _CloseFakeBackend.instances == []


def test_close_rejects_a_worker_that_is_not_yet_accepted(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    registry.transition(worker.name, WorkerState.READY)

    with pytest.raises(ValidationError):
        close(registry, worker.name)

    assert _CloseFakeBackend.instances == []
    assert OtherBackend.instances == []


# -- equalize --


def _accept(registry: Registry, worker: WorkerRecord) -> None:
    for state in (
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
    ):
        registry.transition(worker.name, state)


def test_close_equalizes_the_remaining_column_when_two_or_more_siblings_survive(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    first = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    second = make_worker(tmp_path, pane_ref=TMUX_PANE.format(2), suffix="b")
    third = make_worker(tmp_path, pane_ref=TMUX_PANE.format(3), suffix="c")
    doomed = make_worker(tmp_path, pane_ref=TMUX_PANE.format(4), suffix="d")
    for worker in (first, second, third, doomed):
        registry.add(worker)
        _accept(registry, worker)

    close(registry, doomed.name)

    # The tmux equalize path surfaces as a `pane_heights` read (carrying
    # the column) assembled by `operations.py`, followed by
    # `set_pane_height` writes -- each on its own fresh backend instance, so
    # the filter scans every instance instead of only the last one.
    equalize_calls = [
        call
        for instance in _CloseFakeBackend.instances
        for call in instance.calls
        if call["operation"] == "pane_heights"
    ]
    assert len(equalize_calls) == 1
    assert equalize_calls[0]["column_pane_refs"] == [
        TMUX_PANE.format(1),
        TMUX_PANE.format(2),
        TMUX_PANE.format(3),
    ]


def test_close_does_not_equalize_when_one_or_fewer_siblings_survive(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    survivor = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    doomed = make_worker(tmp_path, pane_ref=TMUX_PANE.format(2), suffix="b")
    for worker in (survivor, doomed):
        registry.add(worker)
        _accept(registry, worker)

    close(registry, doomed.name)

    equalize_calls = [
        call
        for instance in _CloseFakeBackend.instances
        for call in instance.calls
        if call["operation"] in ("pane_heights", "equalize")
    ]
    assert equalize_calls == []


def test_close_survives_an_equalize_backend_error_and_reports_a_warning(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    first = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    second = make_worker(tmp_path, pane_ref=TMUX_PANE.format(2), suffix="b")
    doomed = make_worker(tmp_path, pane_ref=TMUX_PANE.format(3), suffix="c")
    for worker in (first, second, doomed):
        registry.add(worker)
        _accept(registry, worker)

    class BrokenEqualizeBackend(_CloseFakeBackend):
        instances: ClassVar[list["_CloseFakeBackend"]] = []

        def run(self, request: dict) -> dict:
            self.calls.append(dict(request))
            if request["operation"] == "pane_heights":
                raise BackendError("resize-pane failed")
            return {"ok": True}

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": BrokenEqualizeBackend, "iterm2": OtherBackend},
    )

    result = close(registry, doomed.name)

    assert result["state"] == WorkerState.CLOSED.value
    assert result["equalize_warning"] == "resize-pane failed"


# -- close guard against live children --


def test_close_rejects_a_worker_with_a_live_child(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    parent = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="parent")
    child = make_worker(
        tmp_path,
        pane_ref=TMUX_PANE.format(2),
        suffix="child",
        parent_worker_name=parent.name,
    )
    registry.add(parent)
    registry.add(child)
    _accept(registry, parent)
    _accept(registry, child)

    with pytest.raises(ValidationError):
        close(registry, parent.name)

    assert _CloseFakeBackend.instances == []
    assert OtherBackend.instances == []


def test_close_succeeds_once_all_children_are_closed(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    parent = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="parent")
    child = make_worker(
        tmp_path,
        pane_ref=TMUX_PANE.format(2),
        suffix="child",
        parent_worker_name=parent.name,
    )
    registry.add(parent)
    registry.add(child)
    _accept(registry, parent)
    _accept(registry, child)
    close(registry, child.name)

    result = close(registry, parent.name)

    assert result["state"] == WorkerState.CLOSED.value


def test_close_is_blocked_by_a_failed_child_that_still_holds_a_pane(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    parent = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="parent")
    child = make_worker(
        tmp_path,
        pane_ref=TMUX_PANE.format(2),
        suffix="child",
        parent_worker_name=parent.name,
    )
    registry.add(parent)
    registry.add(child)
    _accept(registry, parent)
    registry.transition(child.name, WorkerState.READY)
    registry.transition(child.name, WorkerState.FAILED)

    with pytest.raises(ValidationError):
        close(registry, parent.name)

    assert registry.get(child.name).pane_ref == TMUX_PANE.format(2)


def test_close_enforces_bottom_up_order_across_a_three_level_chain(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    grandparent = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="gp")
    parent = make_worker(
        tmp_path,
        pane_ref=TMUX_PANE.format(2),
        suffix="p",
        parent_worker_name=grandparent.name,
    )
    child = make_worker(
        tmp_path,
        pane_ref=TMUX_PANE.format(3),
        suffix="c",
        parent_worker_name=parent.name,
    )
    for worker in (grandparent, parent, child):
        registry.add(worker)
        _accept(registry, worker)

    with pytest.raises(ValidationError):
        close(registry, grandparent.name)
    with pytest.raises(ValidationError):
        close(registry, parent.name)

    close(registry, child.name)
    close(registry, parent.name)
    close(registry, grandparent.name)

    assert registry.get(grandparent.name).state is WorkerState.CLOSED


def test_close_error_lists_blocking_child_names_in_sorted_order(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    parent = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="parent")
    child_z = make_worker(
        tmp_path,
        pane_ref=TMUX_PANE.format(2),
        suffix="zzz",
        parent_worker_name=parent.name,
    )
    child_a = make_worker(
        tmp_path,
        pane_ref=TMUX_PANE.format(3),
        suffix="aaa",
        parent_worker_name=parent.name,
    )
    for worker in (parent, child_z, child_a):
        registry.add(worker)
        _accept(registry, worker)

    with pytest.raises(ValidationError) as excinfo:
        close(registry, parent.name)

    message = str(excinfo.value)
    assert child_a.name in message
    assert child_z.name in message
    assert message.index(child_a.name) < message.index(child_z.name)


# -- close CAS against concurrent mutation --


def test_close_fails_with_conflict_when_the_record_changes_between_snapshot_and_final_transition(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a", window_ref="@1"
    )
    registry.add(worker)
    _accept(registry, worker)

    class RacingBackend(_CloseFakeBackend):
        instances: ClassVar[list["_CloseFakeBackend"]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "close":
                registry.transition(
                    worker.name, WorkerState.FAILED
                )  # interleaves after the pre-destruction check, before the final CAS
            return super().run(request)

    monkeypatch.setattr(
        operations_module, "BACKENDS", {"tmux": RacingBackend, "iterm2": OtherBackend}
    )

    with pytest.raises(ConflictError):
        close(registry, worker.name)

    current = registry.get(worker.name)
    assert current.state is WorkerState.FAILED  # keeps the racing transition
    assert current.pane_ref is None  # compensating clear for the destroyed pane
    # This path only detaches pane_ref --
    # window_ref is preserved, unlike the success path (_mark_closed).
    assert current.window_ref == "@1"


def test_close_conflict_compensation_failure_preserves_the_conflict_error_code(
    tmp_path: Path, monkeypatch
) -> None:
    """When the CLOSED transition races into a ConflictError and the
    best-effort detach_pane() compensation that follows it *also* fails,
    the reported `error.code` must stay `CONFLICT` (the original failure)
    rather than getting swapped for the compensation's own
    RegistryIOError -- while the compensation failure itself is folded
    into the message instead of being silently dropped."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a", window_ref="@1"
    )
    registry.add(worker)
    _accept(registry, worker)

    class RacingBackend(_CloseFakeBackend):
        instances: ClassVar[list["_CloseFakeBackend"]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "close":
                registry.transition(worker.name, WorkerState.FAILED)
            return super().run(request)

    monkeypatch.setattr(
        operations_module, "BACKENDS", {"tmux": RacingBackend, "iterm2": OtherBackend}
    )

    real_mutate = Registry.mutate
    call_count = 0

    def flaky_mutate(
        self: Registry,
        name: str,
        mutation: Callable[[WorkerRecord], None],
        *,
        expected_updated_at: str | None = None,
    ) -> WorkerRecord:
        nonlocal call_count
        call_count += 1
        # Registry.transition() is implemented on top of mutate(), so calls
        # go: (1) RacingBackend's racing FAILED transition, (2) the real
        # _mark_closed mutation -- let both through to naturally produce the
        # ConflictError from the CAS mismatch. (3) the ConflictError's own
        # detach_pane compensation -- this is the one under test, so fail it.
        if call_count == 3:
            raise RegistryIOError("simulated compensation write failure")
        return real_mutate(
            self, name, mutation, expected_updated_at=expected_updated_at
        )

    monkeypatch.setattr(Registry, "mutate", flaky_mutate)

    with pytest.raises(ConflictError) as excinfo:
        close(registry, worker.name)

    assert "simulated compensation write failure" in str(excinfo.value)
    current = registry.get(worker.name)
    assert current.state is WorkerState.FAILED
    # The compensating detach_pane() never landed (its own write failed), so
    # pane_ref is stuck pointing at the already-destroyed pane -- a known,
    # narrower double-fault, not silent data loss.
    assert current.pane_ref == TMUX_PANE.format(1)


def test_close_refuses_to_touch_the_pane_when_the_record_changes_before_the_snapshot_read(
    tmp_path: Path, monkeypatch
) -> None:
    """close() takes a single snapshot read, so a mutation landing before
    that one read is simply observed by it. Here the worker transitions to FAILED
    before the snapshot is taken, so `may_close` rejects it outright
    (ValidationError, not ConflictError) and the pane is never touched --
    the invariant this test guards (no pane op on a stale/racing record)
    still holds, just via a different error type."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    _accept(registry, worker)

    original_list = Registry.list
    call_count = {"n": 0}

    def racing_list(self):
        call_count["n"] += 1
        if call_count["n"] == 1:  # the one snapshot read inside close()
            registry.transition(worker.name, WorkerState.FAILED)
        return original_list(self)

    monkeypatch.setattr(Registry, "list", racing_list)

    with pytest.raises(ValidationError):
        close(registry, worker.name)

    assert _CloseFakeBackend.instances == []  # never touched the pane
    assert registry.get(worker.name).state is WorkerState.FAILED


# -- iterm2 dispatch parity (representative subset) --


def test_close_equalizes_the_remaining_column_when_two_or_more_siblings_survive_iterm2(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    first = make_worker(tmp_path, pane_ref=ITERM2_PANE.format(1), suffix="a")
    second = make_worker(tmp_path, pane_ref=ITERM2_PANE.format(2), suffix="b")
    third = make_worker(tmp_path, pane_ref=ITERM2_PANE.format(3), suffix="c")
    doomed = make_worker(tmp_path, pane_ref=ITERM2_PANE.format(4), suffix="d")
    for worker in (first, second, third, doomed):
        registry.add(worker)
        _accept(registry, worker)

    close(registry, doomed.name)

    equalize_calls = [
        call
        for call in OtherBackend.instances[-1].calls
        if call["operation"] == "equalize"
    ]
    assert len(equalize_calls) == 1
    assert equalize_calls[0]["column_pane_refs"] == [
        ITERM2_PANE.format(1),
        ITERM2_PANE.format(2),
        ITERM2_PANE.format(3),
    ]
    assert _CloseFakeBackend.instances == []


def test_close_succeeds_once_all_children_are_closed_iterm2(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    parent = make_worker(tmp_path, pane_ref=ITERM2_PANE.format(1), suffix="parent")
    child = make_worker(
        tmp_path,
        pane_ref=ITERM2_PANE.format(2),
        suffix="child",
        parent_worker_name=parent.name,
    )
    registry.add(parent)
    registry.add(child)
    _accept(registry, parent)
    _accept(registry, child)
    close(registry, child.name)

    result = close(registry, parent.name)

    assert result["state"] == WorkerState.CLOSED.value
    assert OtherBackend.instances[-1].calls[-1] == {
        "operation": "close",
        "pane_ref": ITERM2_PANE.format(1),
    }
    assert _CloseFakeBackend.instances == []


def test_close_enforces_bottom_up_order_across_a_three_level_chain_iterm2(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    grandparent = make_worker(tmp_path, pane_ref=ITERM2_PANE.format(1), suffix="gp")
    parent = make_worker(
        tmp_path,
        pane_ref=ITERM2_PANE.format(2),
        suffix="p",
        parent_worker_name=grandparent.name,
    )
    child = make_worker(
        tmp_path,
        pane_ref=ITERM2_PANE.format(3),
        suffix="c",
        parent_worker_name=parent.name,
    )
    for worker in (grandparent, parent, child):
        registry.add(worker)
        _accept(registry, worker)

    with pytest.raises(ValidationError):
        close(registry, grandparent.name)
    with pytest.raises(ValidationError):
        close(registry, parent.name)

    close(registry, child.name)
    close(registry, parent.name)
    close(registry, grandparent.name)

    assert registry.get(grandparent.name).state is WorkerState.CLOSED
    assert _CloseFakeBackend.instances == []


# -- window frame save/restore --


class FramingBackend(_CloseFakeBackend):
    instances: ClassVar[list["_CloseFakeBackend"]] = []
    frame: ClassVar[dict] = {
        "ok": True,
        "x": 10.0,
        "y": 20.0,
        "width": 300.0,
        "height": 400.0,
    }

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        operation = request["operation"]
        if operation == "verify":
            return type(self).verify_responses[request["pane_ref"]]
        if operation == "get_window_frame":
            return dict(type(self).frame)
        return {"ok": True}


def test_close_saves_and_restores_the_window_frame_around_pane_destruction(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, pane_ref=ITERM2_PANE.format(1), suffix="a", window_ref="win-1"
    )
    registry.add(worker)
    _accept(registry, worker)

    class LocalFramingBackend(FramingBackend):
        instances: ClassVar[list["_CloseFakeBackend"]] = []

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _CloseFakeBackend, "iterm2": LocalFramingBackend},
    )

    close(registry, worker.name)

    # `BACKENDS[backend]()` is instantiated fresh per `terminal_ops.*` call
    # (see `last_spawn_call()` in test_usecase_spawn.py for the same
    # pattern) -- flatten every instance's one-call `.calls` list, in
    # instantiation order, to see the full call sequence.
    calls = [
        call for instance in LocalFramingBackend.instances for call in instance.calls
    ]
    ops = [call["operation"] for call in calls]
    assert (
        ops.index("get_window_frame")
        < ops.index("close")
        < ops.index("set_window_frame")
    )
    get_call = calls[ops.index("get_window_frame")]
    assert get_call["window_ref"] == "win-1"
    set_call = calls[ops.index("set_window_frame")]
    assert set_call == {
        "operation": "set_window_frame",
        "window_ref": "win-1",
        "x": 10.0,
        "y": 20.0,
        "width": 300.0,
        "height": 400.0,
    }


def test_close_restores_the_window_frame_even_when_the_final_transition_conflicts(
    tmp_path: Path, monkeypatch
) -> None:
    """Frame restore must span the `ConflictError` raise path (L36-39), not
    just the normal return -- the pane is already destroyed by the time
    that race is detected, regardless of which exit `close()` takes."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, pane_ref=ITERM2_PANE.format(1), suffix="a", window_ref="win-1"
    )
    registry.add(worker)
    _accept(registry, worker)

    class RacingFramingBackend(FramingBackend):
        instances: ClassVar[list["_CloseFakeBackend"]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "close":
                registry.transition(
                    worker.name, WorkerState.FAILED
                )  # interleaves right after destruction, before the final CAS
            return super().run(request)

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _CloseFakeBackend, "iterm2": RacingFramingBackend},
    )

    with pytest.raises(ConflictError):
        close(registry, worker.name)

    ops = [
        call["operation"]
        for instance in RacingFramingBackend.instances
        for call in instance.calls
    ]
    assert "set_window_frame" in ops


def test_close_survives_a_window_frame_read_failure_without_failing_the_close(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, pane_ref=ITERM2_PANE.format(1), suffix="a", window_ref="win-1"
    )
    registry.add(worker)
    _accept(registry, worker)

    class NoFrameBackend(_CloseFakeBackend):
        instances: ClassVar[list["_CloseFakeBackend"]] = []

        def run(self, request: dict) -> dict:
            self.calls.append(dict(request))
            if request["operation"] == "get_window_frame":
                return {"ok": True}  # missing x/y/width/height -> BackendError
            return {"ok": True}

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _CloseFakeBackend, "iterm2": NoFrameBackend},
    )

    result = close(registry, worker.name)

    assert result["state"] == WorkerState.CLOSED.value
    ops = [
        call["operation"]
        for instance in NoFrameBackend.instances
        for call in instance.calls
    ]
    assert "set_window_frame" not in ops


def test_close_tmux_path_never_calls_window_frame_ops_even_when_window_ref_is_set(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a", window_ref="win-1"
    )
    registry.add(worker)
    _accept(registry, worker)

    close(registry, worker.name)

    ops = [
        call["operation"]
        for instance in _CloseFakeBackend.instances
        for call in instance.calls
    ]
    assert "get_window_frame" not in ops
    assert "set_window_frame" not in ops


def test_close_still_attempts_a_frame_restore_when_terminal_ops_close_itself_raises(
    tmp_path: Path, monkeypatch
) -> None:
    """F9 (frame save/restore dedup): with the shared `preserve_window_frame`
    context manager, `terminal_ops.close()` now runs *inside* the block, so
    a `BackendError` raised there still triggers a best-effort restore
    attempt before propagating -- unlike the pre-F9 code, where the
    try/finally only wrapped the code *after* the `close()` call, so a
    `close()` raise skipped the restore entirely. This is an intentional,
    unrequested-by-spec behavior change flagged in the card's
    SPEC-DEVIATIONS (get_window_frame must run before the destructive call
    regardless, so the two calls have to share one bracket either way); the
    `BackendError` itself still propagates uncaught, matching the pre-F9
    behavior."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, pane_ref=ITERM2_PANE.format(1), suffix="a", window_ref="win-1"
    )
    registry.add(worker)
    _accept(registry, worker)

    class CloseBlowsUpBackend(FramingBackend):
        instances: ClassVar[list["_CloseFakeBackend"]] = []

        def run(self, request: dict) -> dict:
            self.calls.append(dict(request))
            if request["operation"] == "close":
                raise BackendError("close blew up")
            if request["operation"] == "get_window_frame":
                return dict(type(self).frame)
            return {"ok": True}

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _CloseFakeBackend, "iterm2": CloseBlowsUpBackend},
    )

    with pytest.raises(BackendError):
        close(registry, worker.name)

    ops = [
        call["operation"]
        for instance in CloseBlowsUpBackend.instances
        for call in instance.calls
    ]
    assert (
        ops.index("get_window_frame")
        < ops.index("close")
        < ops.index("set_window_frame")
    )


# -- residual TOCTOU: a child spawned between the guard and pane destruction --


def test_close_reports_children_that_appeared_between_the_guard_and_pane_destruction(
    tmp_path: Path, monkeypatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    doomed = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="parent")
    registry.add(doomed)
    _accept(registry, doomed)

    class SpawningBackend(_CloseFakeBackend):
        instances: ClassVar[list["_CloseFakeBackend"]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "close":
                # Reproduces a separate process spawning a child after the
                # guard passes but before pane destruction -- registry.add
                # does not change doomed's updated_at, so the final CAS
                # slips past it.
                late_child = make_worker(
                    tmp_path,
                    pane_ref=TMUX_PANE.format(2),
                    suffix="late-child",
                    parent_worker_name=doomed.name,
                )
                registry.add(late_child)
            return super().run(request)

    monkeypatch.setattr(
        operations_module, "BACKENDS", {"tmux": SpawningBackend, "iterm2": OtherBackend}
    )

    result = close(registry, doomed.name)

    assert result["state"] == WorkerState.CLOSED.value
    assert "ccw-x-review-late-child" in result["orphaned_children_warning"]


def test_reconcile_clears_pane_ref_on_pane_vanish_and_unblocks_parent_close(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    parent = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="parent")
    child = make_worker(
        tmp_path,
        pane_ref=TMUX_PANE.format(2),
        suffix="child",
        parent_worker_name=parent.name,
    )
    registry.add(parent)
    registry.add(child)
    _accept(registry, parent)
    _accept(registry, child)
    assert parent.pane_ref is not None
    assert child.pane_ref is not None
    _CloseFakeBackend.verify_responses[parent.pane_ref] = {"ok": True, "exists": True}
    _CloseFakeBackend.verify_responses[child.pane_ref] = {"ok": True, "exists": False}

    result = reconcile(registry)

    assert [item["name"] for item in result["reconciled"]] == [child.name]
    assert result["pane_cleared"] == []
    assert registry.get(child.name).state is WorkerState.FAILED
    assert registry.get(child.name).pane_ref is None

    result = close(registry, parent.name)

    assert result["state"] == WorkerState.CLOSED.value


def test_reconcile_clears_a_stale_pane_on_an_already_failed_child_and_unblocks_parent_close(
    tmp_path: Path,
) -> None:
    """The card's motivating scenario: a child reaches FAILED (e.g. via
    `sukuna state --state failed`) while its pane is still alive, per
    CLAUDE.md's "leave the pane for a human to inspect" guidance. The human
    later closes the pane by hand, leaving the child FAILED with a stale
    `pane_ref`. Before this fix, `reconcilable_workers()` already excludes a
    FAILED worker (no outgoing transition), so `reconcile()` never touched
    it and the parent's `close()` stayed permanently blocked by
    `alive_pane_holders()`'s live-child guard. `stale_pane_candidates()`
    covers exactly this gap."""
    registry = Registry(tmp_path / "registry.json")
    parent = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="parent")
    child = make_worker(
        tmp_path,
        pane_ref=TMUX_PANE.format(2),
        suffix="child",
        parent_worker_name=parent.name,
    )
    registry.add(parent)
    registry.add(child)
    _accept(registry, parent)
    registry.transition(child.name, WorkerState.READY)
    registry.transition(child.name, WorkerState.FAILED)
    assert registry.get(child.name).pane_ref == TMUX_PANE.format(2)

    with pytest.raises(ValidationError):
        close(registry, parent.name)

    _CloseFakeBackend.verify_responses[TMUX_PANE.format(1)] = {
        "ok": True,
        "exists": True,
    }
    _CloseFakeBackend.verify_responses[TMUX_PANE.format(2)] = {
        "ok": True,
        "exists": False,
    }

    result = reconcile(registry)

    assert result["reconciled"] == []
    assert [item["name"] for item in result["pane_cleared"]] == [child.name]
    updated_child = registry.get(child.name)
    assert updated_child.state is WorkerState.FAILED
    assert updated_child.pane_ref is None

    close_result = close(registry, parent.name)

    assert close_result["state"] == WorkerState.CLOSED.value

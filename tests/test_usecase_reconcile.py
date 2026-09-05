from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from _terminal_fakes import ITERM2_PANE, TMUX_PANE
from _terminal_fakes import make_worker as _make_worker

import sukuna.infrastructure.terminal.operations as operations_module
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.domain.service.reconcile_filter import (
    may_manually_fail,
    reconcilable_workers,
    stale_pane_candidates,
)
from sukuna.errors import BackendError
from sukuna.infrastructure.registry import Registry
from sukuna.usecase.reconcile import reconcile


class _ReconcileFakeBackend:
    instances: ClassVar[list["_ReconcileFakeBackend"]] = []
    responses: ClassVar[dict[str, dict]] = {}
    raises: ClassVar[set[str]] = set()

    def __init__(self) -> None:
        self.calls: list[dict] = []
        type(self).instances.append(self)

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        pane_ref = request["pane_ref"]
        if pane_ref in type(self).raises:
            raise BackendError("verify failed")
        return type(self).responses[pane_ref]


class OtherBackend(_ReconcileFakeBackend):
    instances: ClassVar[list["_ReconcileFakeBackend"]] = []
    responses: ClassVar[dict[str, dict]] = {}
    raises: ClassVar[set[str]] = set()


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch):
    for backend in (_ReconcileFakeBackend, OtherBackend):
        backend.instances = []
        backend.responses = {}
        backend.raises = set()
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _ReconcileFakeBackend, "iterm2": OtherBackend},
    )
    yield


def make_worker(
    repo: Path,
    *,
    pane_ref: str | None,
    suffix: str,
    window_ref: str | None = None,
) -> WorkerRecord:
    return _make_worker(
        repo,
        suffix=suffix,
        pane_ref=pane_ref,
        window_ref=window_ref,
        parent_session_id="parent-1",
    )


def advance(registry: Registry, name: str, *states: WorkerState) -> None:
    for state in states:
        registry.transition(name, state)


def test_reconcile_fails_a_worker_whose_pane_is_gone(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a", window_ref="@1"
    )
    registry.add(worker)
    advance(registry, worker.name, WorkerState.READY)
    assert worker.pane_ref is not None
    _ReconcileFakeBackend.responses[worker.pane_ref] = {"ok": True, "exists": False}

    result = reconcile(registry)

    assert [item["name"] for item in result["reconciled"]] == [worker.name]
    assert result["pane_cleared"] == []
    updated = registry.get(worker.name)
    assert updated.state is WorkerState.FAILED
    assert updated.pane_ref is None
    # reconcile's detach_pane() calls leave window_ref alone.
    assert updated.window_ref == "@1"


def test_reconcile_leaves_a_worker_whose_pane_still_exists(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    advance(registry, worker.name, WorkerState.READY)
    assert worker.pane_ref is not None
    _ReconcileFakeBackend.responses[worker.pane_ref] = {"ok": True, "exists": True}

    result = reconcile(registry)

    assert result["reconciled"] == []
    assert result["pane_cleared"] == []
    assert registry.get(worker.name).state is WorkerState.READY


def test_reconcile_excludes_a_worker_with_no_pane_ref(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=None, suffix="a")
    registry.add(worker)

    result = reconcile(registry)

    assert result["reconciled"] == []
    assert result["pane_cleared"] == []
    assert _ReconcileFakeBackend.instances == []
    assert OtherBackend.instances == []


def test_reconcile_excludes_a_closed_worker_without_verifying(tmp_path: Path) -> None:
    """A CLOSED worker is excluded from both passes -- `reconcilable_workers()`
    by its terminal state, `stale_pane_candidates()` because CLOSED is not in
    `_STALE_PANE_STATES` (in production `close()` always clears `pane_ref` on
    the CLOSED transition itself; a manually-crafted CLOSED record with a
    lingering `pane_ref`, as here, must still never be verified)."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    advance(
        registry,
        worker.name,
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
        WorkerState.CLOSED,
    )

    result = reconcile(registry)

    assert result == {"reconciled": [], "pane_cleared": []}
    assert _ReconcileFakeBackend.instances == []


@pytest.mark.parametrize("terminal_state", [WorkerState.FAILED, WorkerState.TIMED_OUT])
def test_reconcile_verifies_but_does_not_transition_a_terminal_worker_whose_pane_still_exists(
    tmp_path: Path, terminal_state: WorkerState
) -> None:
    """FAILED/TIMED_OUT workers are excluded from the first pass
    (`reconcilable_workers()`, no outgoing transition) but are candidates
    for the second (`stale_pane_candidates()`). When the pane still exists,
    the second pass must leave both state and `pane_ref` untouched -- only
    a vanished pane triggers a clear."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    advance(registry, worker.name, terminal_state)
    assert worker.pane_ref is not None
    _ReconcileFakeBackend.responses[worker.pane_ref] = {"ok": True, "exists": True}

    result = reconcile(registry)

    assert result == {"reconciled": [], "pane_cleared": []}
    updated = registry.get(worker.name)
    assert updated.state is terminal_state
    assert updated.pane_ref == TMUX_PANE.format(1)


def test_reconcile_fails_an_accepted_worker_without_raising(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    advance(
        registry,
        worker.name,
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
    )
    assert worker.pane_ref is not None
    _ReconcileFakeBackend.responses[worker.pane_ref] = {"ok": True, "exists": False}

    result = reconcile(registry)

    assert [item["name"] for item in result["reconciled"]] == [worker.name]
    assert registry.get(worker.name).state is WorkerState.FAILED


def test_reconcile_leaves_worker_untouched_when_verify_itself_fails(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    advance(registry, worker.name, WorkerState.READY)
    assert worker.pane_ref is not None
    _ReconcileFakeBackend.raises.add(worker.pane_ref)

    result = reconcile(registry)

    assert result == {"reconciled": [], "pane_cleared": []}
    assert registry.get(worker.name).state is WorkerState.READY


def test_reconcile_skips_a_worker_with_an_invalid_response_without_halting_the_batch(
    tmp_path: Path,
) -> None:
    """One worker's malformed `verify` response must not abort the whole
    sweep -- `parse_backend_response` translates the pydantic error into a
    `BackendError`, which is a `CrossBufferError` the existing `except
    CrossBufferError: continue` already catches, so the other worker still
    gets reconciled."""
    registry = Registry(tmp_path / "registry.json")
    good = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    bad = make_worker(tmp_path, pane_ref=TMUX_PANE.format(2), suffix="b")
    registry.add(good)
    registry.add(bad)
    advance(registry, good.name, WorkerState.READY)
    advance(registry, bad.name, WorkerState.READY)
    assert good.pane_ref is not None
    assert bad.pane_ref is not None
    _ReconcileFakeBackend.responses[good.pane_ref] = {"ok": True, "exists": False}
    _ReconcileFakeBackend.responses[bad.pane_ref] = {
        "ok": True
    }  # missing "exists" -> invalid VerifyResponse

    result = reconcile(registry)

    assert [item["name"] for item in result["reconciled"]] == [good.name]
    assert registry.get(good.name).state is WorkerState.FAILED
    assert registry.get(bad.name).state is WorkerState.READY


def test_reconcile_skips_a_worker_that_reached_a_terminal_state_concurrently(
    tmp_path: Path, monkeypatch
) -> None:
    """A worker can reach a terminal state (a concurrent close, or another
    reconcile sweep) between this sweep's `registry.list()` snapshot and
    its own `mutate` call. The racing worker's `verify` call is the hook
    for reproducing that: its side effect (advancing the *real* registry to
    FAILED) runs between the snapshot and the mutate, exercising the
    production `mutate` + `expected_updated_at` CAS path directly rather
    than mocking `mutate` itself. The racing worker must be skipped without
    halting the sweep, and the worker after it must still be reconciled."""
    registry = Registry(tmp_path / "registry.json")
    racing = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    good = make_worker(tmp_path, pane_ref=TMUX_PANE.format(2), suffix="b")
    registry.add(racing)
    registry.add(good)
    advance(registry, racing.name, WorkerState.READY)
    advance(registry, good.name, WorkerState.READY)
    assert racing.pane_ref is not None
    assert good.pane_ref is not None
    _ReconcileFakeBackend.responses[racing.pane_ref] = {"ok": True, "exists": False}
    _ReconcileFakeBackend.responses[good.pane_ref] = {"ok": True, "exists": False}

    racing_pane_ref = racing.pane_ref
    original_run = _ReconcileFakeBackend.run

    def run_with_concurrent_transition(self, request: dict) -> dict:
        if request["pane_ref"] == racing_pane_ref:
            registry.transition(racing.name, WorkerState.FAILED)
        return original_run(self, request)

    monkeypatch.setattr(_ReconcileFakeBackend, "run", run_with_concurrent_transition)

    result = reconcile(registry)

    assert [item["name"] for item in result["reconciled"]] == [good.name]
    assert result["pane_cleared"] == []
    updated_racing = registry.get(racing.name)
    assert updated_racing.state is WorkerState.FAILED
    assert updated_racing.pane_ref is not None  # skip must not clear the pane
    updated_good = registry.get(good.name)
    assert updated_good.state is WorkerState.FAILED
    assert updated_good.pane_ref is None


def test_reconcile_dispatches_tmux_and_iterm2_shaped_panes_to_their_backend(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    tmux_worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    iterm2_worker = make_worker(tmp_path, pane_ref=ITERM2_PANE.format(1), suffix="b")
    registry.add(tmux_worker)
    registry.add(iterm2_worker)
    advance(registry, tmux_worker.name, WorkerState.READY)
    advance(registry, iterm2_worker.name, WorkerState.READY)
    assert tmux_worker.pane_ref is not None
    assert iterm2_worker.pane_ref is not None
    _ReconcileFakeBackend.responses[tmux_worker.pane_ref] = {"ok": True, "exists": True}
    OtherBackend.responses[iterm2_worker.pane_ref] = {"ok": True, "exists": True}

    reconcile(registry)

    assert _ReconcileFakeBackend.instances[-1].calls[-1] == {
        "operation": "verify",
        "pane_ref": tmux_worker.pane_ref,
    }
    assert OtherBackend.instances[-1].calls[-1] == {
        "operation": "verify",
        "pane_ref": iterm2_worker.pane_ref,
    }


def test_reconcile_fails_and_clears_pane_for_an_iterm2_shaped_pane_ref(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=ITERM2_PANE.format(1), suffix="a")
    registry.add(worker)
    advance(registry, worker.name, WorkerState.READY)
    assert worker.pane_ref is not None
    OtherBackend.responses[worker.pane_ref] = {"ok": True, "exists": False}

    result = reconcile(registry)

    assert [item["name"] for item in result["reconciled"]] == [worker.name]
    assert result["pane_cleared"] == []
    updated = registry.get(worker.name)
    assert updated.state is WorkerState.FAILED
    assert updated.pane_ref is None
    assert (
        _ReconcileFakeBackend.instances == []
    )  # exclusivity: the tmux fake was never called at all
    assert OtherBackend.instances[-1].calls[-1] == {
        "operation": "verify",
        "pane_ref": ITERM2_PANE.format(1),
    }


def test_reconcilable_workers_filters_pane_ref_and_terminal_state(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    live = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    no_pane = make_worker(tmp_path, pane_ref=None, suffix="b")
    failed = make_worker(tmp_path, pane_ref=TMUX_PANE.format(2), suffix="c")
    for worker in (live, no_pane, failed):
        registry.add(worker)
    advance(registry, live.name, WorkerState.READY)
    advance(registry, failed.name, WorkerState.FAILED)

    candidates = reconcilable_workers(registry.list())

    assert {worker.name for worker in candidates} == {live.name}


def test_reconcilable_workers_excludes_an_unmanaged_worker(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    managed = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    unmanaged = make_worker(tmp_path, pane_ref=TMUX_PANE.format(2), suffix="b")
    unmanaged.managed = False
    for worker in (managed, unmanaged):
        registry.add(worker)
    advance(registry, managed.name, WorkerState.READY)
    advance(registry, unmanaged.name, WorkerState.READY)

    candidates = reconcilable_workers(registry.list())

    assert {worker.name for worker in candidates} == {managed.name}


def test_reconcile_leaves_an_unmanaged_worker_untouched(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    worker.managed = False
    registry.add(worker)
    advance(registry, worker.name, WorkerState.READY)
    assert worker.pane_ref is not None
    _ReconcileFakeBackend.responses[worker.pane_ref] = {"ok": True, "exists": False}

    result = reconcile(registry)

    assert result == {"reconciled": [], "pane_cleared": []}
    assert _ReconcileFakeBackend.instances == []
    assert registry.get(worker.name).state is WorkerState.READY


# -- stale_pane_candidates() / pane_cleared (card: reconcile stale pane_ref) --


@pytest.mark.parametrize("terminal_state", [WorkerState.FAILED, WorkerState.TIMED_OUT])
def test_stale_pane_candidates_includes_terminal_states_with_a_live_pane_ref(
    tmp_path: Path, terminal_state: WorkerState
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    advance(registry, worker.name, terminal_state)

    candidates = stale_pane_candidates(registry.list())

    assert {w.name for w in candidates} == {worker.name}


def test_stale_pane_candidates_excludes_a_closed_worker(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    advance(
        registry,
        worker.name,
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
        WorkerState.CLOSED,
    )

    candidates = stale_pane_candidates(registry.list())

    assert candidates == []


def test_stale_pane_candidates_excludes_a_non_terminal_worker(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    advance(registry, worker.name, WorkerState.READY)

    candidates = stale_pane_candidates(registry.list())

    assert candidates == []


def test_stale_pane_candidates_excludes_a_terminal_worker_with_no_pane_ref(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=None, suffix="a")
    registry.add(worker)
    advance(registry, worker.name, WorkerState.FAILED)

    candidates = stale_pane_candidates(registry.list())

    assert candidates == []


def test_stale_pane_candidates_excludes_an_unmanaged_worker(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    worker.managed = False
    registry.add(worker)
    advance(registry, worker.name, WorkerState.FAILED)

    candidates = stale_pane_candidates(registry.list())

    assert candidates == []


# -- may_manually_fail() (manual `sukuna state --state
# failed` guard, consumed by usecase/state.py) --


def test_may_manually_fail_allows_a_worker_with_no_pane_ref(tmp_path: Path) -> None:
    worker = make_worker(tmp_path, pane_ref=None, suffix="a")

    assert may_manually_fail(worker, pane_confirmed_gone=False) is True


def test_may_manually_fail_allows_a_worker_whose_pane_verified_gone(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")

    assert may_manually_fail(worker, pane_confirmed_gone=True) is True


def test_may_manually_fail_denies_a_worker_whose_pane_still_exists(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")

    assert may_manually_fail(worker, pane_confirmed_gone=False) is False


@pytest.mark.parametrize("terminal_state", [WorkerState.FAILED, WorkerState.TIMED_OUT])
def test_reconcile_clears_a_stale_pane_ref_without_changing_state(
    tmp_path: Path, terminal_state: WorkerState
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(
        tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a", window_ref="@1"
    )
    registry.add(worker)
    advance(registry, worker.name, terminal_state)
    assert worker.pane_ref is not None
    _ReconcileFakeBackend.responses[worker.pane_ref] = {"ok": True, "exists": False}

    result = reconcile(registry)

    assert result["reconciled"] == []
    assert [item["name"] for item in result["pane_cleared"]] == [worker.name]
    updated = registry.get(worker.name)
    assert updated.state is terminal_state
    assert updated.pane_ref is None
    # reconcile's detach_pane() calls leave window_ref alone.
    assert updated.window_ref == "@1"


def test_reconcile_leaves_a_stale_candidates_pane_ref_when_verify_itself_fails(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    advance(registry, worker.name, WorkerState.FAILED)
    assert worker.pane_ref is not None
    _ReconcileFakeBackend.raises.add(worker.pane_ref)

    result = reconcile(registry)

    assert result == {"reconciled": [], "pane_cleared": []}
    assert registry.get(worker.name).pane_ref == TMUX_PANE.format(1)


def test_reconcile_skips_a_stale_pane_clear_when_the_record_changes_concurrently(
    tmp_path: Path, monkeypatch
) -> None:
    """The same CAS protection as the first pass: if the record's
    `updated_at` moves between this sweep's snapshot and the clear-only
    `mutate` call, the clear must be skipped rather than blindly applied
    over an update it never observed."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)
    advance(registry, worker.name, WorkerState.FAILED)
    assert worker.pane_ref is not None
    _ReconcileFakeBackend.responses[worker.pane_ref] = {"ok": True, "exists": False}

    worker_pane_ref = worker.pane_ref
    original_run = _ReconcileFakeBackend.run

    def run_with_concurrent_touch(self, request: dict) -> dict:
        if request["pane_ref"] == worker_pane_ref:
            registry.mutate(
                worker.name,
                lambda current: setattr(
                    current, "updated_at", "2099-01-01T00:00:00+00:00"
                ),
            )
        return original_run(self, request)

    monkeypatch.setattr(_ReconcileFakeBackend, "run", run_with_concurrent_touch)

    result = reconcile(registry)

    assert result == {"reconciled": [], "pane_cleared": []}
    assert registry.get(worker.name).pane_ref == TMUX_PANE.format(1)


def test_reconcile_pane_cleared_and_reconciled_are_disjoint_in_one_sweep(
    tmp_path: Path,
) -> None:
    """A worker whose state changes to FAILED during the first pass must not
    also be examined by the second pass in the same sweep -- both passes
    filter one `registry.list()` snapshot taken before either pass runs, so
    the just-failed worker (non-terminal at snapshot time) is absent from
    `stale_pane_candidates()`'s view of that same snapshot."""
    registry = Registry(tmp_path / "registry.json")
    transitioning = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    already_stale = make_worker(tmp_path, pane_ref=TMUX_PANE.format(2), suffix="b")
    registry.add(transitioning)
    registry.add(already_stale)
    advance(registry, transitioning.name, WorkerState.READY)
    advance(registry, already_stale.name, WorkerState.FAILED)
    assert transitioning.pane_ref is not None
    assert already_stale.pane_ref is not None
    _ReconcileFakeBackend.responses[transitioning.pane_ref] = {
        "ok": True,
        "exists": False,
    }
    _ReconcileFakeBackend.responses[already_stale.pane_ref] = {
        "ok": True,
        "exists": False,
    }

    result = reconcile(registry)

    assert [item["name"] for item in result["reconciled"]] == [transitioning.name]
    assert [item["name"] for item in result["pane_cleared"]] == [already_stale.name]
    assert registry.get(transitioning.name).state is WorkerState.FAILED
    assert registry.get(transitioning.name).pane_ref is None
    assert registry.get(already_stale.name).state is WorkerState.FAILED
    assert registry.get(already_stale.name).pane_ref is None


def test_reconcile_prune_false_by_default_omits_purged_key(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    registry = Registry(tmp_path / "registry.json")

    result = reconcile(registry)

    assert "purged" not in result


def test_reconcile_prune_true_adds_the_purged_key_and_uses_the_injected_now(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    registry = Registry(tmp_path / "registry.json")
    now = datetime(2026, 8, 30, tzinfo=UTC)
    ancient = make_worker(tmp_path, pane_ref=None, suffix="a")
    ancient.state = WorkerState.CLOSED
    ancient.updated_at = (now - timedelta(days=1000)).isoformat()
    recent = make_worker(tmp_path, pane_ref=None, suffix="b")
    recent.state = WorkerState.CLOSED
    recent.updated_at = (now - timedelta(days=1)).isoformat()
    registry.add(ancient)
    registry.add(recent)

    result = reconcile(registry, prune=True, now=now)

    assert [item["name"] for item in result["purged"]] == [ancient.name]
    assert {record.name for record in registry.list()} == {recent.name}

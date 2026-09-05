from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

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
import sukuna.usecase.spawn as spawn_module
from sukuna.domain.entity.pane import SplitDirection
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.domain.mapper.registry_mapper import RegistryState
from sukuna.domain.mapper.terminal_mapper import SpawnResult
from sukuna.domain.service.resize_convergence import MAX_RESIZE_ATTEMPTS
from sukuna.domain.service.spawn_placement import (
    children_of,
    choose_anchor,
    equalize_target,
)
from sukuna.domain.service.spawn_preflight import SpawnSpec
from sukuna.errors import (
    BackendError,
    ConflictError,
    ModelCatalogError,
    RegistryIOError,
    ValidationError,
)
from sukuna.infrastructure.registry import Registry
from sukuna.usecase.spawn import spawn_many


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch):
    # `usecase/spawn.py` dispatches through `infrastructure.terminal.operations`,
    # which is the single place `BACKENDS` needs patching now.
    install_backends(monkeypatch, {"tmux": RecordingBackend, "iterm2": OtherBackend})
    patch_pane_resolution(monkeypatch, spawn_module)
    yield


def make_worker(
    repo: Path,
    *,
    parent_session_id: str | None = "parent-1",
    pane_ref: str | None = None,
    suffix: str | None = None,
    parent_worker_name: str | None = None,
) -> WorkerRecord:
    return _make_worker(
        repo,
        parent_session_id=parent_session_id,
        pane_ref=pane_ref,
        suffix=suffix,
        parent_worker_name=parent_worker_name,
    )


def last_spawn_call() -> dict:
    return [
        call
        for instance in RecordingBackend.instances
        for call in instance.calls
        if call["operation"] == "spawn"
    ][-1]


def resize_calls() -> list[dict]:
    """`active_pane_width` application is a separate `resize` wire call
    (issued by `operations.py`, on a fresh `RecordingBackend` instance)
    after the `spawn` call -- mirrors `equalize_calls()` above."""
    return [
        call
        for instance in RecordingBackend.instances
        for call in instance.calls
        if call["operation"] == "resize"
    ]


def equalize_calls() -> list[dict]:
    """tmux equalize is assembled by `operations.py` as a `pane_heights`
    read plus `set_pane_height` writes -- the read carries the column and
    fires exactly once per equalize, so it is the marker for "the
    equalize path ran with this column" (the write fan-out itself is pinned
    in tests/test_terminal_operations.py). iTerm2 keeps the one-shot
    `equalize` op, filtered by `other_equalize_calls()` below."""
    return [
        call
        for instance in RecordingBackend.instances
        for call in instance.calls
        if call["operation"] == "pane_heights"
    ]


def make_spec(repo: Path, **overrides) -> SpawnSpec:
    kwargs: dict[str, Any] = {
        "role": "review",
        "repo": repo,
        "worktree": repo,
        "goal": None,
        "parent_worker": None,
        "active_pane_width": 50,
        "model": None,
    }
    kwargs.update(overrides)
    return SpawnSpec(**kwargs)


def do_spawn(
    registry: Registry,
    repo: Path,
    *,
    parent_session_id: str | None = "parent-1",
    api_key: str | None = None,
    **overrides,
) -> dict:
    """Spawn exactly one worker via the batch entrypoint and return its
    result dict -- mirrors the pre-batch single-item `spawn()` helper so the
    placement-matrix tests below stay focused on placement, not batching."""
    result = spawn_many(
        registry,
        [make_spec(repo, **overrides)],
        parent_session_id=parent_session_id,
        api_key=api_key,
    )
    (entry,) = result["succeeded"]
    return entry["worker"]


def do_spawn_expect_failure(
    registry: Registry,
    repo: Path,
    *,
    parent_session_id: str | None = "parent-1",
    api_key: str | None = None,
    **overrides,
) -> dict:
    """Spawn exactly one worker expected to fail at runtime and return its
    `failed[]` entry."""
    result = spawn_many(
        registry,
        [make_spec(repo, **overrides)],
        parent_session_id=parent_session_id,
        api_key=api_key,
    )
    (entry,) = result["failed"]
    return entry


def test_failed_worker_does_not_corrupt_the_next_anchor(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    failed = make_worker(tmp_path, suffix="a")  # no pane_ref: never got past STARTING
    registry.add(failed)
    registry.transition(failed.name, WorkerState.FAILED)

    do_spawn(registry, tmp_path)

    call = last_spawn_call()
    assert call["anchor_pane_ref"] == "orchestrator-pane"
    assert call["split_direction"] == "horizontal"


def test_backend_mismatch_is_rejected_before_any_pane_operation(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    # Environment resolves to "tmux" (fixture default) but this worker's
    # pane looks like an iTerm2 session id.
    live = make_worker(tmp_path, suffix="a", pane_ref=ITERM2_PANE.format(0))
    registry.add(live)
    registry.transition(live.name, WorkerState.READY)

    entry = do_spawn_expect_failure(registry, tmp_path)

    assert entry["error"]["code"] == ValidationError.code
    assert "worker_name" not in entry  # no registry.add() was reached for this element
    assert RecordingBackend.instances == []
    assert OtherBackend.instances == []


def test_spawn_records_the_given_parent_session_and_worker(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    worker = do_spawn(
        registry,
        tmp_path,
        parent_session_id="orchestrator-session-id",
        parent_worker=None,
    )

    assert worker["parent_session_id"] == "orchestrator-session-id"
    assert worker["parent_worker_name"] is None


def test_spawn_stores_goal_but_never_forwards_it_to_the_command(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    worker = do_spawn(registry, tmp_path, goal="investigate the flaky test")

    assert worker["goal"] == "investigate the flaky test"
    command = last_spawn_call()["command"]
    assert "investigate the flaky test" not in command
    assert "--goal" not in command


def test_spawn_stores_model_and_forwards_it_to_the_command(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    worker = do_spawn(registry, tmp_path, model="sonnet-5")

    assert worker["model"] == "sonnet-5"
    command = last_spawn_call()["command"]
    assert "--model sonnet-5" in command


def test_model_validation_is_skipped_without_an_api_key_even_with_a_model_set(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[dict] = []

    def fake_fetch(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(spawn_module, "fetch_model_ids", fake_fetch)
    registry = Registry(tmp_path / "registry.json")

    worker = do_spawn(registry, tmp_path, model="anything-goes", api_key=None)

    assert worker["model"] == "anything-goes"
    assert calls == []


def test_model_validation_cache_hit_skips_the_network_fetch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        spawn_module, "read_cached_model_ids", lambda path: ["claude-sonnet-5"]
    )
    calls: list[dict] = []

    def fake_fetch(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(spawn_module, "fetch_model_ids", fake_fetch)
    registry = Registry(tmp_path / "registry.json")

    worker = do_spawn(
        registry, tmp_path, model="claude-sonnet-5", api_key="sk-ant-test"
    )

    assert worker["model"] == "claude-sonnet-5"
    assert calls == []


def test_model_validation_cache_miss_then_fetch_succeeds_and_the_model_is_found(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(spawn_module, "read_cached_model_ids", lambda path: [])
    write_calls: list[list[str]] = []
    monkeypatch.setattr(
        spawn_module,
        "write_cached_model_ids",
        lambda ids, path: write_calls.append(ids),
    )
    monkeypatch.setattr(
        spawn_module, "fetch_model_ids", lambda **kw: ["claude-sonnet-5"]
    )
    registry = Registry(tmp_path / "registry.json")

    worker = do_spawn(
        registry, tmp_path, model="claude-sonnet-5", api_key="sk-ant-test"
    )

    assert worker["model"] == "claude-sonnet-5"
    assert write_calls == [["claude-sonnet-5"]]


def test_model_validation_cache_miss_then_fetch_succeeds_but_model_not_found(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(spawn_module, "read_cached_model_ids", lambda path: [])
    write_calls: list[list[str]] = []
    monkeypatch.setattr(
        spawn_module,
        "write_cached_model_ids",
        lambda ids, path: write_calls.append(ids),
    )
    monkeypatch.setattr(spawn_module, "fetch_model_ids", lambda **kw: ["claude-opus-5"])
    registry = Registry(tmp_path / "registry.json")

    entry = do_spawn_expect_failure(
        registry, tmp_path, model="bogus-model", api_key="sk-ant-test"
    )

    assert entry["error"]["code"] == ValidationError.code
    # cache-update-on-not-found: the fresh catalog is written even though
    # the requested model wasn't in it.
    assert write_calls == [["claude-opus-5"]]


def test_model_validation_not_found_after_fetch_does_not_affect_a_found_sibling_spec(
    tmp_path: Path, monkeypatch
) -> None:
    """Card scenario 4 is explicit that a `model` missing from the
    re-fetched catalog only fails that spec -- a sibling spec in the same
    batch whose `model` the same fetch *did* find must still succeed."""
    monkeypatch.setattr(spawn_module, "read_cached_model_ids", lambda path: [])
    monkeypatch.setattr(spawn_module, "write_cached_model_ids", lambda ids, path: None)
    monkeypatch.setattr(
        spawn_module, "fetch_model_ids", lambda **kw: ["claude-sonnet-5"]
    )
    registry = Registry(tmp_path / "registry.json")
    specs = [
        make_spec(tmp_path, model="claude-sonnet-5"),  # found
        make_spec(tmp_path, model="bogus-model"),  # not found
    ]

    result = spawn_many(
        registry, specs, parent_session_id="parent-1", api_key="sk-ant-test"
    )

    assert len(result["succeeded"]) == 1
    assert result["succeeded"][0]["worker"]["model"] == "claude-sonnet-5"
    assert len(result["failed"]) == 1
    failed_entry = result["failed"][0]
    assert failed_entry["error"]["code"] == ValidationError.code
    assert "worker_name" not in failed_entry


def test_model_validation_cache_write_failure_does_not_fail_the_batch(
    tmp_path: Path, monkeypatch
) -> None:
    """The cache is a disposable derived artifact — an
    `OSError` from `write_cached_model_ids` (disk full, permissions) after a
    successful fetch must not escape `spawn_many()` and reject the whole
    batch. Validation proceeds on the in-hand fresh catalog: the found
    spec still spawns, the not-found spec still fails per-element with
    `ValidationError`, exactly as if the write had succeeded."""
    monkeypatch.setattr(spawn_module, "read_cached_model_ids", lambda path: [])

    def fail_write(ids, path):
        raise OSError("disk full")

    monkeypatch.setattr(spawn_module, "write_cached_model_ids", fail_write)
    monkeypatch.setattr(
        spawn_module, "fetch_model_ids", lambda **kw: ["claude-sonnet-5"]
    )
    registry = Registry(tmp_path / "registry.json")
    specs = [
        make_spec(tmp_path, model="claude-sonnet-5"),  # found
        make_spec(tmp_path, model="bogus-model"),  # not found
    ]

    result = spawn_many(
        registry, specs, parent_session_id="parent-1", api_key="sk-ant-test"
    )

    assert len(result["succeeded"]) == 1
    assert result["succeeded"][0]["worker"]["model"] == "claude-sonnet-5"
    assert len(result["failed"]) == 1
    failed_entry = result["failed"][0]
    assert failed_entry["error"]["code"] == ValidationError.code
    assert "worker_name" not in failed_entry


def test_model_validation_fetch_failure_fails_only_the_affected_spec(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(spawn_module, "read_cached_model_ids", lambda path: [])
    write_calls: list[list[str]] = []
    monkeypatch.setattr(
        spawn_module,
        "write_cached_model_ids",
        lambda ids, path: write_calls.append(ids),
    )

    def fail_fetch(**kwargs):
        raise ModelCatalogError("offline")

    monkeypatch.setattr(spawn_module, "fetch_model_ids", fail_fetch)
    registry = Registry(tmp_path / "registry.json")

    entry = do_spawn_expect_failure(
        registry, tmp_path, model="claude-sonnet-5", api_key="sk-ant-test"
    )

    assert entry["error"]["code"] == ModelCatalogError.code
    assert write_calls == []


def test_model_validation_fetch_failure_does_not_affect_a_cache_hit_sibling_spec(
    tmp_path: Path, monkeypatch
) -> None:
    """The card is explicit that a fetch failure only fails the specs that
    missed the cache -- a sibling spec in the same batch whose `model` was
    already cached must succeed regardless of the fetch failure."""
    monkeypatch.setattr(
        spawn_module, "read_cached_model_ids", lambda path: ["claude-sonnet-5"]
    )
    write_calls: list[list[str]] = []
    monkeypatch.setattr(
        spawn_module,
        "write_cached_model_ids",
        lambda ids, path: write_calls.append(ids),
    )

    def fail_fetch(**kwargs):
        raise ModelCatalogError("offline")

    monkeypatch.setattr(spawn_module, "fetch_model_ids", fail_fetch)
    registry = Registry(tmp_path / "registry.json")
    specs = [
        make_spec(tmp_path, model="claude-sonnet-5"),  # cache hit
        make_spec(tmp_path, model="claude-opus-5"),  # cache miss
    ]

    result = spawn_many(
        registry, specs, parent_session_id="parent-1", api_key="sk-ant-test"
    )

    assert len(result["succeeded"]) == 1
    assert result["succeeded"][0]["worker"]["model"] == "claude-sonnet-5"
    assert len(result["failed"]) == 1
    failed_entry = result["failed"][0]
    assert failed_entry["error"]["code"] == ModelCatalogError.code
    assert "worker_name" not in failed_entry  # registry.add() never ran for it
    assert write_calls == []


def test_model_validation_dedupes_multiple_uncached_models_into_one_fetch_call(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(spawn_module, "read_cached_model_ids", lambda path: [])
    monkeypatch.setattr(spawn_module, "write_cached_model_ids", lambda ids, path: None)
    fetch_calls: list[dict] = []

    def fake_fetch(**kwargs):
        fetch_calls.append(kwargs)
        return ["claude-sonnet-5", "claude-opus-5"]

    monkeypatch.setattr(spawn_module, "fetch_model_ids", fake_fetch)
    registry = Registry(tmp_path / "registry.json")
    specs = [
        make_spec(tmp_path, model="claude-sonnet-5"),
        make_spec(tmp_path, model="claude-opus-5"),
    ]

    result = spawn_many(
        registry, specs, parent_session_id="parent-1", api_key="sk-ant-test"
    )

    assert result["failed"] == []
    assert len(result["succeeded"]) == 2
    assert len(fetch_calls) == 1


def test_spawn_fails_with_conflict_when_the_record_changes_between_add_and_replace(
    tmp_path: Path, monkeypatch
) -> None:
    """Simulates a separate process mutating the worker (e.g. via
    `registry.transition()`) in the window between this spawn's
    `registry.add()` and its final `registry.replace()`; the backend call
    is the only hook available in-process, so the race is injected there."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    class RacingBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "spawn":
                (racing_worker,) = registry.list()
                registry.transition(racing_worker.name, WorkerState.FAILED)
            return super().run(request)

    import sukuna.errors as errors_module

    monkeypatch.setattr(
        operations_module, "BACKENDS", {"tmux": RacingBackend, "iterm2": OtherBackend}
    )

    entry = do_spawn_expect_failure(registry, tmp_path)

    assert entry["error"]["code"] == errors_module.ConflictError.code
    (worker,) = registry.list()
    assert worker.state is WorkerState.FAILED
    assert worker.pane_ref == TMUX_PANE.format(1)
    assert worker.window_ref == "win-1"
    # ConflictError edge: worker and pane were already created before the
    # race was detected, so the failed entry must carry the worker's name
    # -- otherwise the orphaned pane has no way to be tracked down.
    assert entry["worker_name"] == worker.name


def test_spawn_conflict_compensation_failure_preserves_the_conflict_error_code(
    tmp_path: Path, monkeypatch
) -> None:
    """When the same race as
    above happens *and* the compensating `registry.mutate()` (re-attaching
    the pane to the conflicting record) itself hits a registry failure,
    the reported `error.code` must stay `CONFLICT` -- not get swapped for
    the mutate's own failure code -- while the mutate failure itself is
    folded into the message instead of being silently dropped."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    class RacingBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "spawn":
                (racing_worker,) = registry.list()
                registry.transition(racing_worker.name, WorkerState.FAILED)
            return super().run(request)

    monkeypatch.setattr(
        operations_module, "BACKENDS", {"tmux": RacingBackend, "iterm2": OtherBackend}
    )

    original_mutate = Registry.mutate
    calls = {"n": 0}

    def flaky_mutate(
        self: Registry,
        name: str,
        mutation: Callable[[WorkerRecord], None],
        **kwargs: Any,
    ) -> WorkerRecord:
        calls["n"] += 1
        # Call 1 = the racing backend's own `registry.transition(...,
        # FAILED)` above (which is itself implemented via `mutate()`) --
        # that one must succeed so the race (and thus the ConflictError)
        # actually happens. Call 2 = the compensating `registry.mutate()`
        # that re-attaches the pane to the conflicting record -- fail
        # only this one.
        if calls["n"] == 2:
            raise RegistryIOError("simulated write failure")
        return original_mutate(self, name, mutation, **kwargs)

    monkeypatch.setattr(Registry, "mutate", flaky_mutate)

    entry = do_spawn_expect_failure(registry, tmp_path)

    assert entry["error"]["code"] == ConflictError.code
    assert "simulated write failure" in entry["error"]["message"]
    (worker,) = registry.list()
    assert worker.state is WorkerState.FAILED
    # The compensating mutate() never ran, so the pane was never re-attached
    # to the record -- a known, narrower double-fault, not silent data loss.
    assert worker.pane_ref is None


# -- registry.add..registry.replace failure compensation (fix 2) --


def test_spawn_verify_backend_error_fails_the_worker(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    class BrokenVerifyBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "verify":
                raise BackendError("verify unavailable")
            return super().run(request)

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": BrokenVerifyBackend, "iterm2": OtherBackend},
    )

    entry = do_spawn_expect_failure(registry, tmp_path)

    assert entry["error"]["code"] == BackendError.code
    (worker,) = registry.list()
    assert worker.state is WorkerState.FAILED
    assert entry["worker_name"] == worker.name


def test_spawn_failed_transition_failure_preserves_the_original_error_code(
    tmp_path: Path, monkeypatch
) -> None:
    """When the runtime failure's own compensating
    `registry.transition(..., FAILED)` call (in `_spawn_one()`'s
    `except CrossBufferError` block) itself hits a registry failure, the
    reported `error.code` must stay the *original* failure's code
    (`BACKEND_ERROR` here, from the broken verify backend below) rather
    than getting swapped for the transition's own failure code -- while
    the transition failure itself is folded into the message instead of
    being silently dropped."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    class BrokenVerifyBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "verify":
                raise BackendError("verify unavailable")
            return super().run(request)

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": BrokenVerifyBackend, "iterm2": OtherBackend},
    )

    def flaky_transition(
        self: Registry, name: str, target: WorkerState, **kwargs: Any
    ) -> WorkerRecord:
        raise RegistryIOError("simulated write failure")

    monkeypatch.setattr(Registry, "transition", flaky_transition)

    entry = do_spawn_expect_failure(registry, tmp_path)

    assert entry["error"]["code"] == BackendError.code
    assert "verify unavailable" in entry["error"]["message"]
    assert "simulated write failure" in entry["error"]["message"]
    # The compensating transition() never ran, so the record is really
    # stuck in STARTING on disk -- a known, narrower double-fault (the same
    # write that would have flipped it to FAILED is the one that failed),
    # not silent data loss; the batch itself still degrades to a `failed`
    # entry instead of aborting.
    (worker,) = registry.list()
    assert worker.state is WorkerState.STARTING
    assert entry["worker_name"] == worker.name


def test_invalid_spawn_result_fails_the_worker_and_raises_backend_error(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    class MalformedSpawnBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def _spawn(self, request: dict) -> dict:
            return {
                "ok": True,
                "pane_ref": "",
                "window_ref": None,
            }  # empty pane_ref: fails min_length=1

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": MalformedSpawnBackend, "iterm2": OtherBackend},
    )

    entry = do_spawn_expect_failure(registry, tmp_path)

    assert entry["error"]["code"] == BackendError.code
    (worker,) = registry.list()
    assert worker.state is WorkerState.FAILED


def test_missing_anchor_fails_the_worker(tmp_path: Path, monkeypatch) -> None:
    """Regression test: before fix 2, this `ValidationError` (a
    `CrossBufferError` subclass, raised outside any `try`) reached the
    caller but left the worker stuck in STARTING forever."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    monkeypatch.setattr(spawn_module, "orchestrator_pane_ref", lambda backend: None)

    entry = do_spawn_expect_failure(registry, tmp_path)

    assert entry["error"]["code"] == ValidationError.code
    (worker,) = registry.list()
    assert worker.state is WorkerState.FAILED


# -- registry-driven recursive placement --


def test_spawn_splits_the_orchestrator_pane_horizontally_when_it_has_no_children(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    do_spawn(registry, tmp_path)

    call = last_spawn_call()
    assert call["anchor_pane_ref"] == "orchestrator-pane"
    assert call["split_direction"] == "horizontal"


def test_spawn_splits_the_last_root_child_vertically_when_root_children_exist(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    root_child = make_worker(
        tmp_path, suffix="root-child", pane_ref=TMUX_PANE.format(1)
    )
    registry.add(root_child)
    registry.transition(root_child.name, WorkerState.READY)
    RecordingBackend.world = {"orchestrator-pane", TMUX_PANE.format(1)}

    do_spawn(registry, tmp_path)

    call = last_spawn_call()
    assert call["anchor_pane_ref"] == TMUX_PANE.format(1)
    assert call["split_direction"] == "vertical"


def test_spawn_forwards_active_pane_width_when_placement_is_horizontal(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    do_spawn(registry, tmp_path, active_pane_width=70)

    call = last_spawn_call()
    assert call["split_direction"] == "horizontal"
    assert "active_pane_width" not in call
    (resize_call,) = resize_calls()
    assert resize_call["percent"] == 70


def test_spawn_defaults_active_pane_width_to_50_when_the_key_is_absent(
    tmp_path: Path, monkeypatch
) -> None:
    """The "absent/null -> 50" default is resolved exactly once, by
    `preflight_spawn_specs()` -- `spawn_many()` has no fallback of its
    own, so this test goes through the real preflight pass rather than
    building a spec dict by hand."""
    monkeypatch.setattr(spawn_module, "load_active_pane_width", lambda: 50)
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    raw_spec = {
        "role": "review",
        "repo": str(tmp_path),
        "worktree": str(tmp_path),
    }

    specs = spawn_module.preflight_spawn_specs([raw_spec], parent_session_id="parent-1")
    result = spawn_many(registry, specs, parent_session_id="parent-1")

    (entry,) = result["succeeded"]
    assert entry["worker"]["state"] == "ready"
    assert "active_pane_width" not in last_spawn_call()
    (resize_call,) = resize_calls()
    assert resize_call["percent"] == 50


def test_spawn_ignores_active_pane_width_when_placement_is_vertical(
    tmp_path: Path,
) -> None:
    """A vertical (stacked) placement silently drops the percent -- no
    warning, no failure (card principle: the option only applies to the
    "0 children -> split the parent horizontally" case)."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    root_child = make_worker(
        tmp_path, suffix="root-child", pane_ref=TMUX_PANE.format(1)
    )
    registry.add(root_child)
    registry.transition(root_child.name, WorkerState.READY)
    RecordingBackend.world = {"orchestrator-pane", TMUX_PANE.format(1)}

    result = do_spawn(registry, tmp_path, active_pane_width=70)

    call = last_spawn_call()
    assert call["split_direction"] == "vertical"
    assert "active_pane_width" not in call
    assert resize_calls() == []
    assert result["state"] == "ready"


def test_spawn_nests_under_a_named_parent_worker_with_no_children(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    parent = make_worker(tmp_path, suffix="parent", pane_ref=TMUX_PANE.format(1))
    registry.add(parent)
    registry.transition(parent.name, WorkerState.READY)
    RecordingBackend.world = {"orchestrator-pane", TMUX_PANE.format(1)}

    do_spawn(registry, tmp_path, parent_worker=parent.name)

    call = last_spawn_call()
    assert call["anchor_pane_ref"] == TMUX_PANE.format(1)
    assert call["split_direction"] == "horizontal"


def test_spawn_nests_under_the_last_grandchild_of_a_named_parent_worker(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    parent = make_worker(tmp_path, suffix="parent", pane_ref=TMUX_PANE.format(1))
    registry.add(parent)
    registry.transition(parent.name, WorkerState.READY)
    first_child = make_worker(
        tmp_path,
        suffix="child-1",
        pane_ref=TMUX_PANE.format(2),
        parent_worker_name=parent.name,
    )
    registry.add(first_child)
    registry.transition(first_child.name, WorkerState.READY)
    last_child = make_worker(
        tmp_path,
        suffix="child-2",
        pane_ref=TMUX_PANE.format(3),
        parent_worker_name=parent.name,
    )
    registry.add(last_child)
    registry.transition(last_child.name, WorkerState.READY)
    RecordingBackend.world = {
        "orchestrator-pane",
        TMUX_PANE.format(1),
        TMUX_PANE.format(2),
        TMUX_PANE.format(3),
    }

    do_spawn(registry, tmp_path, parent_worker=parent.name)

    call = last_spawn_call()
    assert call["anchor_pane_ref"] == TMUX_PANE.format(3)
    assert call["split_direction"] == "vertical"


def test_spawn_falls_back_to_root_when_parent_worker_name_does_not_resolve(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    worker = do_spawn(registry, tmp_path, parent_worker="typo-xxx")

    assert worker["state"] == WorkerState.READY.value
    call = last_spawn_call()
    assert call["anchor_pane_ref"] == "orchestrator-pane"
    assert call["split_direction"] == "horizontal"


def test_spawn_falls_back_to_root_when_named_parent_has_no_pane(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    parent = make_worker(
        tmp_path, suffix="parent"
    )  # no pane_ref: never got past STARTING
    registry.add(parent)
    registry.transition(parent.name, WorkerState.FAILED)

    worker = do_spawn(registry, tmp_path, parent_worker=parent.name)

    assert worker["state"] == WorkerState.READY.value
    call = last_spawn_call()
    assert call["anchor_pane_ref"] == "orchestrator-pane"


def test_spawn_falls_back_to_root_when_named_parent_is_closed(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    parent = make_worker(tmp_path, suffix="parent", pane_ref=TMUX_PANE.format(1))
    registry.add(parent)
    for state in (
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
        WorkerState.CLOSED,
    ):
        registry.transition(parent.name, state)

    worker = do_spawn(registry, tmp_path, parent_worker=parent.name)

    assert worker["state"] == WorkerState.READY.value
    call = last_spawn_call()
    assert call["anchor_pane_ref"] == "orchestrator-pane"


def test_spawn_falls_back_to_root_when_the_named_parents_pane_no_longer_exists(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    root = make_worker(tmp_path, suffix="root", pane_ref=TMUX_PANE.format(1))
    registry.add(root)
    registry.transition(root.name, WorkerState.READY)
    # `named_parent` nests under `root` and its pane is not part of
    # `RecordingBackend.world` -- simulating a pane that was killed outside
    # sukuna's knowledge -- while `root`'s own pane is still alive, so the
    # root fallback anchor differs from the dead child-scope anchor.
    named_parent = make_worker(
        tmp_path,
        suffix="named-parent",
        pane_ref=TMUX_PANE.format(2),
        parent_worker_name=root.name,
    )
    registry.add(named_parent)
    registry.transition(named_parent.name, WorkerState.READY)
    RecordingBackend.world = {"orchestrator-pane", TMUX_PANE.format(1)}

    worker = do_spawn(registry, tmp_path, parent_worker=named_parent.name)

    assert worker["state"] == WorkerState.READY.value
    call = last_spawn_call()
    assert call["anchor_pane_ref"] == TMUX_PANE.format(1)
    assert call["split_direction"] == "vertical"


def test_spawn_fails_the_worker_when_the_orchestrator_pane_itself_no_longer_exists(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    RecordingBackend.world = {TMUX_PANE.format(9)}  # orchestrator pane itself is gone

    entry = do_spawn_expect_failure(registry, tmp_path)

    assert entry["error"]["code"] == ValidationError.code
    (worker,) = registry.list()
    assert worker.state is WorkerState.FAILED
    assert not any(
        call["operation"] == "spawn"
        for instance in RecordingBackend.instances
        for call in instance.calls
    )


def test_spawn_verify_is_called_once_in_the_normal_path(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    do_spawn(registry, tmp_path)

    verify_calls = [
        call
        for instance in RecordingBackend.instances
        for call in instance.calls
        if call["operation"] == "verify"
    ]
    assert len(verify_calls) == 1


def test_spawn_verify_is_called_at_most_twice_even_when_root_fallback_also_fails(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    # `parent` is itself a root-scoped worker, so the root fallback anchor
    # (its own pane, being the only root child) is exactly as dead as the
    # child-scope anchor -- both verify calls fail, and there is no third.
    parent = make_worker(tmp_path, suffix="parent", pane_ref=TMUX_PANE.format(99))
    registry.add(parent)
    registry.transition(parent.name, WorkerState.READY)

    entry = do_spawn_expect_failure(registry, tmp_path, parent_worker=parent.name)

    assert entry["error"]["code"] == ValidationError.code
    verify_calls = [
        call
        for instance in RecordingBackend.instances
        for call in instance.calls
        if call["operation"] == "verify"
    ]
    assert len(verify_calls) == 2
    failed = next(worker for worker in registry.list() if worker.name != parent.name)
    assert failed.state is WorkerState.FAILED


def test_children_of_orders_by_spawn_order_not_by_updated_at() -> None:
    older = make_worker(
        Path("/repo"), suffix="a", pane_ref="%1", parent_worker_name="parent"
    )
    newer = make_worker(
        Path("/repo"), suffix="b", pane_ref="%2", parent_worker_name="parent"
    )
    # spawn order is [older, newer], but `updated_at` deliberately disagrees
    # with that order -- `children_of()` must not be fooled into treating
    # `older` as "last" just because its timestamp is later.
    older.updated_at = "2026-08-22T10:00:00+00:00"
    newer.updated_at = "2026-08-22T09:00:00+00:00"

    children = children_of([older, newer], "parent")

    assert children[-1] is newer


# -- equalize --


def test_equalize_target_returns_none_for_an_empty_column() -> None:
    assert equalize_target([]) is None


def test_equalize_target_returns_pane_refs_in_spawn_order() -> None:
    older = make_worker(
        Path("/repo"), suffix="a", pane_ref="%1", parent_worker_name="parent"
    )
    newer = make_worker(
        Path("/repo"), suffix="b", pane_ref="%2", parent_worker_name="parent"
    )

    assert equalize_target([older, newer]) == ["%1", "%2"]


def test_equalize_target_rejects_a_child_without_a_pane_ref() -> None:
    paneless = make_worker(Path("/repo"), suffix="paneless", pane_ref=None)

    with pytest.raises(ValidationError, match="pane_ref"):
        equalize_target([paneless])


def test_choose_anchor_rejects_a_last_child_without_a_pane_ref() -> None:
    paneless = make_worker(Path("/repo"), suffix="paneless", pane_ref=None)

    with pytest.raises(ValidationError, match="pane_ref"):
        choose_anchor([paneless], "%orchestrator")


def test_vertical_spawn_equalizes_the_column_including_the_new_pane(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    root_child = make_worker(
        tmp_path, suffix="root-child", pane_ref=TMUX_PANE.format(1)
    )
    registry.add(root_child)
    registry.transition(root_child.name, WorkerState.READY)
    RecordingBackend.world = {"orchestrator-pane", TMUX_PANE.format(1)}

    do_spawn(registry, tmp_path)

    calls = equalize_calls()
    assert len(calls) == 1
    new_pane_ref = next(
        member.pane_ref for member in registry.list() if member.name != root_child.name
    )
    assert calls[0]["column_pane_refs"] == [TMUX_PANE.format(1), new_pane_ref]


def test_horizontal_spawn_does_not_equalize(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    do_spawn(registry, tmp_path)

    assert equalize_calls() == []


def test_spawn_survives_an_equalize_backend_error_and_reports_a_warning(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    root_child = make_worker(
        tmp_path, suffix="root-child", pane_ref=TMUX_PANE.format(1)
    )
    registry.add(root_child)
    registry.transition(root_child.name, WorkerState.READY)
    RecordingBackend.world = {"orchestrator-pane", TMUX_PANE.format(1)}

    class BrokenEqualizeBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            self.calls.append(dict(request))
            if request["operation"] == "pane_heights":
                raise BackendError("resize-pane failed")
            operation = request["operation"]
            if operation == "spawn":
                return self._spawn(request)
            if operation == "verify":
                return self._verify(request)
            return {"ok": True}

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": BrokenEqualizeBackend, "iterm2": OtherBackend},
    )

    worker = do_spawn(registry, tmp_path)

    assert worker["state"] == WorkerState.READY.value
    assert worker["equalize_warning"] == "resize-pane failed"


# -- iterm2 dispatch parity (representative subset) --


def other_last_spawn_call() -> dict:
    return [
        call
        for instance in OtherBackend.instances
        for call in instance.calls
        if call["operation"] == "spawn"
    ][-1]


def other_equalize_calls() -> list[dict]:
    return [
        call
        for instance in OtherBackend.instances
        for call in instance.calls
        if call["operation"] == "equalize"
    ]


def test_spawn_splits_the_orchestrator_pane_horizontally_when_it_has_no_children_iterm2(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    monkeypatch.setattr(spawn_module, "detect_backend", lambda: "iterm2")

    do_spawn(registry, tmp_path)

    # `other_last_spawn_call()`, not a raw `.calls[-1]`: a horizontal iTerm2
    # placement issues a percent-convergence `resize()` (and a
    # window-frame save/restore) after the plain `spawn` call, so the very
    # last call on this backend is not `spawn`.
    call = other_last_spawn_call()
    assert call["anchor_pane_ref"] == "orchestrator-pane"
    assert call["split_direction"] == "horizontal"
    assert "active_pane_width" not in call
    assert RecordingBackend.instances == []


def test_spawn_nests_under_the_last_grandchild_of_a_named_parent_worker_iterm2(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    monkeypatch.setattr(spawn_module, "detect_backend", lambda: "iterm2")
    parent = make_worker(tmp_path, suffix="parent", pane_ref=ITERM2_PANE.format(1))
    registry.add(parent)
    registry.transition(parent.name, WorkerState.READY)
    first_child = make_worker(
        tmp_path,
        suffix="child-1",
        pane_ref=ITERM2_PANE.format(2),
        parent_worker_name=parent.name,
    )
    registry.add(first_child)
    registry.transition(first_child.name, WorkerState.READY)
    last_child = make_worker(
        tmp_path,
        suffix="child-2",
        pane_ref=ITERM2_PANE.format(3),
        parent_worker_name=parent.name,
    )
    registry.add(last_child)
    registry.transition(last_child.name, WorkerState.READY)
    OtherBackend.world = {
        "orchestrator-pane",
        ITERM2_PANE.format(1),
        ITERM2_PANE.format(2),
        ITERM2_PANE.format(3),
    }

    do_spawn(registry, tmp_path, parent_worker=parent.name)

    call = other_last_spawn_call()
    assert call["anchor_pane_ref"] == ITERM2_PANE.format(3)
    assert call["split_direction"] == "vertical"
    assert RecordingBackend.instances == []


def test_spawn_falls_back_to_root_when_the_named_parents_pane_no_longer_exists_iterm2(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    monkeypatch.setattr(spawn_module, "detect_backend", lambda: "iterm2")
    root = make_worker(tmp_path, suffix="root", pane_ref=ITERM2_PANE.format(1))
    registry.add(root)
    registry.transition(root.name, WorkerState.READY)
    named_parent = make_worker(
        tmp_path,
        suffix="named-parent",
        pane_ref=ITERM2_PANE.format(2),
        parent_worker_name=root.name,
    )
    registry.add(named_parent)
    registry.transition(named_parent.name, WorkerState.READY)
    OtherBackend.world = {"orchestrator-pane", ITERM2_PANE.format(1)}

    worker = do_spawn(registry, tmp_path, parent_worker=named_parent.name)

    assert worker["state"] == WorkerState.READY.value
    call = other_last_spawn_call()
    assert call["anchor_pane_ref"] == ITERM2_PANE.format(1)
    assert call["split_direction"] == "vertical"
    other_verify_calls = [
        call
        for instance in OtherBackend.instances
        for call in instance.calls
        if call["operation"] == "verify"
    ]
    assert len(other_verify_calls) == 2
    assert RecordingBackend.instances == []


def test_backend_mismatch_is_rejected_before_any_pane_operation_reverse(
    tmp_path: Path, monkeypatch
) -> None:
    """Reverse of `test_backend_mismatch_is_rejected_before_any_pane_operation`:
    the environment resolves to iterm2, but the existing worker's pane looks
    like a tmux pane id -- the comparison in `spawn.py:109` is symmetric, so
    both directions must be rejected before any backend operation runs."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    monkeypatch.setattr(spawn_module, "detect_backend", lambda: "iterm2")
    live = make_worker(tmp_path, suffix="a", pane_ref=TMUX_PANE.format(0))
    registry.add(live)
    registry.transition(live.name, WorkerState.READY)

    entry = do_spawn_expect_failure(registry, tmp_path)

    assert entry["error"]["code"] == ValidationError.code
    assert RecordingBackend.instances == []
    assert OtherBackend.instances == []


def test_backend_mismatch_via_parent_worker_from_another_session_is_rejected(
    tmp_path: Path,
) -> None:
    """`existing_workers()`'s backend-mismatch check in `_spawn_one()` is
    scoped to `parent_session_id`, so it never sees a worker from a
    *different* session -- but `parent_worker` can still name one, and
    `child_placement()`'s `pane_holders` is registry-wide with no
    session/backend scope, so it picks that foreign worker's pane_ref as
    the anchor regardless. This must be rejected before any pane
    operation, the same way a same-session mismatch already is, instead
    of falling back to root."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    # Different session, so `existing_workers(..., parent_session_id="parent-1")`
    # never includes it; current environment resolves to "tmux" (fixture
    # default), so this worker's UUID-shaped pane_ref is a foreign backend.
    foreign = make_worker(
        tmp_path,
        suffix="foreign",
        parent_session_id="other-session",
        pane_ref=ITERM2_PANE.format(0),
    )
    registry.add(foreign)
    registry.transition(foreign.name, WorkerState.READY)

    entry = do_spawn_expect_failure(registry, tmp_path, parent_worker=foreign.name)

    assert entry["error"]["code"] == ValidationError.code
    # Unlike the same-session mismatch tests above, `registry.add()` already
    # ran by the time `_resolve_placement()` rejects the anchor (the
    # session-scoped check up front saw nothing wrong) -- the new worker is
    # on record, best-effort transitioned to FAILED, but never got a pane.
    assert entry["worker_name"]
    failed_worker = registry.get(entry["worker_name"])
    assert failed_worker.state is WorkerState.FAILED
    assert failed_worker.pane_ref is None
    assert failed_worker.parent_worker_name == foreign.name
    # No pane operation -- spawn or verify -- was attempted on either backend.
    assert RecordingBackend.instances == []
    assert OtherBackend.instances == []


def test_vertical_spawn_equalizes_the_column_including_the_new_pane_iterm2(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    monkeypatch.setattr(spawn_module, "detect_backend", lambda: "iterm2")
    root_child = make_worker(
        tmp_path, suffix="root-child", pane_ref=ITERM2_PANE.format(1)
    )
    registry.add(root_child)
    registry.transition(root_child.name, WorkerState.READY)
    OtherBackend.world = {"orchestrator-pane", ITERM2_PANE.format(1)}

    do_spawn(registry, tmp_path)

    calls = other_equalize_calls()
    assert len(calls) == 1
    new_pane_ref = next(
        member.pane_ref for member in registry.list() if member.name != root_child.name
    )
    assert calls[0]["column_pane_refs"] == [ITERM2_PANE.format(1), new_pane_ref]
    assert RecordingBackend.instances == []


# -- batching (spawn as an array-input CLI surface) --


def test_spawn_many_processes_specs_sequentially_so_placement_reflects_prior_elements(
    tmp_path: Path,
) -> None:
    """Two elements in one call: the first has no siblings (root, horizontal
    split of the orchestrator pane); the second must see the first's
    already-committed pane as a root sibling and stack vertically under it
    -- proving the loop re-reads the registry between elements rather than
    computing placement for the whole batch up front."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    result = spawn_many(
        registry,
        [make_spec(tmp_path), make_spec(tmp_path)],
        parent_session_id="parent-1",
    )

    assert [entry["index"] for entry in result["succeeded"]] == [0, 1]
    assert result["failed"] == []
    spawn_calls = [
        call
        for instance in RecordingBackend.instances
        for call in instance.calls
        if call["operation"] == "spawn"
    ]
    assert len(spawn_calls) == 2
    assert spawn_calls[0]["anchor_pane_ref"] == "orchestrator-pane"
    assert spawn_calls[0]["split_direction"] == "horizontal"
    (first, second) = registry.list()
    assert second.name != first.name
    assert spawn_calls[1]["anchor_pane_ref"] == first.pane_ref
    assert spawn_calls[1]["split_direction"] == "vertical"


def test_spawn_many_same_role_multiple_specs_in_one_batch_all_succeed_with_distinct_names(
    tmp_path: Path,
) -> None:
    """Layer 1's name-uniqueness check must not regress the same-role
    multi-spawn pattern this dev-loop itself relies on (a design-review
    role has stacked up to ordinal 6 in real use). Three specs, same
    shard/session/role/repo, must all succeed with three distinct
    ordinals -- not be rejected as colliding."""
    registry = Registry(tmp_path / "registry.json")

    result = spawn_many(
        registry,
        [make_spec(tmp_path), make_spec(tmp_path), make_spec(tmp_path)],
        parent_session_id="parent-1",
    )

    assert result["failed"] == []
    names = [entry["worker"]["name"] for entry in result["succeeded"]]
    assert len(set(names)) == 3
    assert [name.rsplit("-", maxsplit=1)[-1] for name in names] == ["1", "2", "3"]


def test_spawn_many_same_role_multiple_specs_succeed_when_the_floor_exceeds_the_record_count(
    tmp_path: Path,
) -> None:
    """Reproduced end to end: a shard with zero records but an
    `ordinal_high_water_marks` floor already ahead of the record count
    (exactly what a retention purge leaves behind). A Layer 1
    implementation that simulates only the records-view (and not also
    advancing the floor between specs) makes the second spec collide with
    the first on the same ordinal, and the batch would incorrectly fail
    at commit time (`StateError`) instead of succeeding. Both specs must
    succeed, with ordinals 10 and 11."""
    registry = Registry(tmp_path / "registry.json")
    registry.write_state(
        RegistryState(
            workers={}, ordinal_high_water_marks={"parent-1": 10}, last_swept_at=None
        )
    )

    result = spawn_many(
        registry,
        [make_spec(tmp_path), make_spec(tmp_path)],
        parent_session_id="parent-1",
    )

    assert result["failed"] == []
    names = [entry["worker"]["name"] for entry in result["succeeded"]]
    assert [name.rsplit("-", maxsplit=1)[-1] for name in names] == ["10", "11"]


def test_spawn_many_rejects_the_whole_batch_when_two_shard_groups_collide(
    tmp_path: Path,
) -> None:
    """Layer 1's core scenario: two specs, same session/repo/
    role, resolved (by an artificial resolver here -- see
    `tests/test_cli_registry_sharding.py` for the real ROOT+grandchild and
    sister-form reproductions) to two different, both-empty shards. Each
    shard independently computes ordinal=1, so both specs compute the same
    name -- the whole batch must be rejected before either pane operation
    or registry write, not just downgraded to a per-element `failed`
    entry."""
    shard_a = Registry(tmp_path / "a.json")
    shard_b = Registry(tmp_path / "b.json")
    resolution = iter([shard_a, shard_b])

    def resolve(spec: SpawnSpec) -> Registry:
        return next(resolution)

    with pytest.raises(ValidationError):
        spawn_many(
            resolve,
            [make_spec(tmp_path), make_spec(tmp_path)],
            parent_session_id="parent-1",
        )

    assert shard_a.list() == []
    assert shard_b.list() == []
    assert RecordingBackend.instances == []


def test_spawn_after_close_reuses_no_name_and_succeeds(tmp_path: Path) -> None:
    """Closing the sole worker under a session must not make the next
    spawn regenerate the same deterministic name (`worker_name()`) as the
    CLOSED record still on disk -- `Registry.add()` rejects any name
    already present regardless of state, which would permanently block
    the ordinary spawn -> accept -> close -> respawn loop."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    first = do_spawn(registry, tmp_path, parent_session_id="parent-1")
    assert first["name"].endswith("-review-1")
    for state in (
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
        WorkerState.CLOSED,
    ):
        registry.transition(first["name"], state)

    second = do_spawn(registry, tmp_path, parent_session_id="parent-1")

    assert second["name"] != first["name"]
    assert second["name"].endswith("-review-2")


def test_spawn_after_closing_the_first_of_two_workers_does_not_collide_with_the_survivor(
    tmp_path: Path,
) -> None:
    """Spawn two workers (`-review-1`, `-review-2`), close the first while
    the second is still alive -- the next spawn's ordinal must not
    collide with that survivor's name either -- `existing_workers()`
    (used only for the backend-mismatch check) would still report
    ordinal 2 here, colliding with the live second worker."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    first = do_spawn(registry, tmp_path, parent_session_id="parent-1")
    second = do_spawn(registry, tmp_path, parent_session_id="parent-1")
    assert first["name"].endswith("-review-1")
    assert second["name"].endswith("-review-2")
    for state in (
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
        WorkerState.CLOSED,
    ):
        registry.transition(first["name"], state)

    third = do_spawn(registry, tmp_path, parent_session_id="parent-1")

    assert third["name"].endswith("-review-3")
    assert third["name"] not in (first["name"], second["name"])


def test_spawn_many_runtime_failure_does_not_block_the_rest_of_the_batch(
    tmp_path: Path, monkeypatch
) -> None:
    """A runtime failure on element 0 (a malformed backend spawn response,
    here injected only on the first backend `spawn` call -- unlike a
    backend-mismatch failure, this doesn't leave behind a pane-holding
    worker that would keep blocking every subsequent element too) must not
    stop element 1 from being attempted."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    class FlakyOnceBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []
        spawn_attempts = 0

        def _spawn(self, request: dict) -> dict:
            type(self).spawn_attempts += 1
            if type(self).spawn_attempts == 1:
                return {
                    "ok": True,
                    "pane_ref": "",
                    "window_ref": None,
                }  # malformed -> BackendError
            return super()._spawn(request)

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": FlakyOnceBackend, "iterm2": OtherBackend},
    )

    result = spawn_many(
        registry,
        [make_spec(tmp_path), make_spec(tmp_path)],
        parent_session_id="parent-1",
    )

    assert [entry["index"] for entry in result["failed"]] == [0]
    assert [entry["index"] for entry in result["succeeded"]] == [1]
    assert result["failed"][0]["error"]["code"] == BackendError.code


def _assert_visible_at_second_spawn(
    snapshot: list[tuple[str, WorkerState, str | None]],
) -> None:
    # Two records are visible: the first element already committed as READY
    # with its pane_ref set (its `registry.replace()` already ran), and the
    # second element already `registry.add()`-ed as STARTING (no pane_ref
    # yet -- its own backend spawn call is the one about to happen).
    assert len(snapshot) == 2
    first_name, first_state, first_pane_ref = snapshot[0]
    second_name, second_state, second_pane_ref = snapshot[1]
    assert first_state is WorkerState.READY
    assert first_pane_ref is not None
    assert second_state is WorkerState.STARTING
    assert second_pane_ref is None
    assert first_name != second_name


def test_spawn_many_commits_each_element_to_the_registry_as_it_goes(
    tmp_path: Path, monkeypatch
) -> None:
    """Registry writes happen per-element (not batched at the end): by the
    time the second element's pane is being created, the first element's
    `registry.add()`/`registry.replace()` must already be durably visible
    to an independent `Registry` handle -- verified here with a spy backend
    that snapshots the registry right before the second `spawn` backend
    call."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    snapshots: list[list[tuple[str, WorkerState, str | None]]] = []

    class SpyBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "spawn":
                spawn_calls_so_far = sum(
                    1
                    for instance in type(self).instances
                    for call in instance.calls
                    if call["operation"] == "spawn"
                )
                if (
                    spawn_calls_so_far == 1
                ):  # about to perform the *second* element's spawn
                    snapshots.append(
                        [
                            (w.name, w.state, w.pane_ref)
                            for w in Registry(registry_path).list()
                        ]
                    )
            return super().run(request)

    monkeypatch.setattr(
        operations_module, "BACKENDS", {"tmux": SpyBackend, "iterm2": OtherBackend}
    )

    spawn_many(
        registry,
        [make_spec(tmp_path), make_spec(tmp_path)],
        parent_session_id="parent-1",
    )

    (snapshot,) = snapshots
    _assert_visible_at_second_spawn(snapshot)


def test_spawn_many_conflict_error_entry_includes_the_created_worker_name(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    class RacingBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "spawn":
                (racing_worker,) = registry.list()
                registry.transition(racing_worker.name, WorkerState.FAILED)
            return super().run(request)

    monkeypatch.setattr(
        operations_module, "BACKENDS", {"tmux": RacingBackend, "iterm2": OtherBackend}
    )

    result = spawn_many(registry, [make_spec(tmp_path)], parent_session_id="parent-1")

    (entry,) = result["failed"]
    (worker,) = registry.list()
    assert entry["worker_name"] == worker.name


def test_spawn_many_output_shape(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    result = spawn_many(registry, [make_spec(tmp_path)], parent_session_id="parent-1")

    assert set(result.keys()) == {"succeeded", "failed"}
    (entry,) = result["succeeded"]
    assert set(entry.keys()) == {"index", "worker"}
    assert entry["index"] == 0
    assert entry["worker"]["name"]


def test_spawn_many_equalize_warning_lives_under_the_succeeded_worker(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    root_child = make_worker(
        tmp_path, suffix="root-child", pane_ref=TMUX_PANE.format(1)
    )
    registry.add(root_child)
    registry.transition(root_child.name, WorkerState.READY)
    RecordingBackend.world = {"orchestrator-pane", TMUX_PANE.format(1)}

    class BrokenEqualizeBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            self.calls.append(dict(request))
            if request["operation"] == "pane_heights":
                raise BackendError("resize-pane failed")
            operation = request["operation"]
            if operation == "spawn":
                return self._spawn(request)
            if operation == "verify":
                return self._verify(request)
            return {"ok": True}

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": BrokenEqualizeBackend, "iterm2": OtherBackend},
    )

    result = spawn_many(registry, [make_spec(tmp_path)], parent_session_id="parent-1")

    (entry,) = result["succeeded"]
    assert "equalize_warning" not in result
    assert entry["worker"]["equalize_warning"] == "resize-pane failed"


# -- iTerm2 spawn-time percent + window frame save/restore --
# (the active_pane_width convergence retry loop itself now lives in
# usecase/pane_convergence.py, shared with usecase/resize.py -- its own
# unit tests moved to tests/test_pane_convergence.py.)


def test_spawn_iterm2_no_longer_forwards_active_pane_width_to_the_backend_spawn_call(
    tmp_path: Path, monkeypatch
) -> None:
    """The spawn-time inline percent write was measured to have no reliable
    effect (Q1-Q5) -- iTerm2 spawn is now always a plain split; the percent
    is applied afterward via the retry loop instead."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    monkeypatch.setattr(spawn_module, "detect_backend", lambda: "iterm2")

    do_spawn(registry, tmp_path, active_pane_width=70)

    assert "active_pane_width" not in other_last_spawn_call()


def test_spawn_result_stays_a_pure_backend_response_type() -> None:
    """`SpawnResult` must represent only a backend's raw response;
    `pending_active_pane_width` is computed
    by `operations.spawn()` itself and belongs on `SpawnOutcome` instead
    (see `operations_module.SpawnOutcome`), never back on `SpawnResult`."""
    assert "pending_active_pane_width" not in SpawnResult.model_fields


def test_operations_spawn_reports_pending_active_pane_width_for_iterm2() -> None:
    """Pins the `terminal_ops.spawn()` contract directly (card F18): the
    decision of whether a backend applies `active_pane_width` inline or
    defers it lives entirely in `infrastructure.terminal.operations`, not
    in any caller. iTerm2 never applies it inline, so the caller gets it
    back via `pending_active_pane_width` to apply as its own follow-up."""
    OtherBackend.instances = []
    OtherBackend.world = {"orchestrator-pane"}

    result = operations_module.spawn(
        backend="iterm2",
        command="claude",
        anchor_pane_ref="orchestrator-pane",
        split_direction=SplitDirection.HORIZONTAL,
        active_pane_width=70,
    )

    assert "active_pane_width" not in other_last_spawn_call()
    assert result.pending_active_pane_width == 70


def test_operations_spawn_reports_pending_active_pane_width_for_tmux_too() -> None:
    """tmux defers to a `pending_active_pane_width` follow-up exactly like
    iTerm2 (see
    `test_operations_spawn_reports_pending_active_pane_width_for_iterm2`
    above and tests/test_terminal_operations.py for the `spawn()` contract
    pinned in isolation)."""
    RecordingBackend.instances = []
    RecordingBackend.world = {"orchestrator-pane"}

    result = operations_module.spawn(
        backend="tmux",
        command="claude",
        anchor_pane_ref="orchestrator-pane",
        split_direction=SplitDirection.HORIZONTAL,
        active_pane_width=70,
    )

    assert "active_pane_width" not in last_spawn_call()
    assert resize_calls() == []
    assert result.pending_active_pane_width == 70


def test_operations_spawn_reports_no_pending_width_when_none_requested() -> None:
    OtherBackend.instances = []
    OtherBackend.world = {"orchestrator-pane"}

    result = operations_module.spawn(
        backend="iterm2",
        command="claude",
        anchor_pane_ref="orchestrator-pane",
        split_direction=SplitDirection.VERTICAL,
        active_pane_width=None,
    )

    assert result.pending_active_pane_width is None


def test_spawn_tmux_applies_active_pane_width_via_resize_ops_and_never_touches_frame_ops(
    tmp_path: Path,
) -> None:
    """tmux applies `active_pane_width` via its own `resize_context` +
    `resize` wire calls, issued by
    `usecase.spawn_shared.apply_pending_active_pane_width` (via
    `converge_active_pane_width`) rather than inline inside
    `operations.spawn()`, but still never touches the iTerm2-only
    window-frame save/restore dance."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    do_spawn(
        registry, tmp_path, active_pane_width=70
    )  # fixture default backend is tmux

    assert "active_pane_width" not in last_spawn_call()
    (resize_call,) = resize_calls()
    assert resize_call["percent"] == 70
    ops = [
        call["operation"]
        for instance in RecordingBackend.instances
        for call in instance.calls
    ]
    assert "get_window_frame" not in ops
    assert "set_window_frame" not in ops
    assert OtherBackend.instances == []


def test_spawn_tmux_survives_a_resize_context_failure_and_reports_a_warning(
    tmp_path: Path, monkeypatch
) -> None:
    """A `resize_context` failure during the post-split follow-up must not
    fail the whole spawn and strand a live, untracked worker with no
    `pane_ref` for `reconcile` to repair -- exercised through
    `usecase.spawn_shared.apply_pending_active_pane_width` (mirrors
    `test_spawn_survives_a_convergence_resize_backend_error_and_reports_a_warning`
    below, for the iTerm2 path)."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    class ResizeContextFailsBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def _resize_context(self, request: dict) -> dict:
            raise BackendError("no space for new pane")

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": ResizeContextFailsBackend, "iterm2": OtherBackend},
    )

    worker = do_spawn(
        registry, tmp_path, active_pane_width=70
    )  # fixture default backend is tmux

    assert worker["state"] == WorkerState.READY.value
    assert worker["pane_ref"] is not None
    assert "no space for new pane" in worker["resize_warning"]


def test_spawn_tmux_clamps_active_pane_width_and_reports_the_warning(
    tmp_path: Path, monkeypatch
) -> None:
    """tmux's resize-time clamp policy
    (`domain.service.pane_width.compute_clamped_percent`, pinned in
    isolation by tests/test_terminal_operations.py) reaches the spawned
    worker's `resize_warning` via `usecase.spawn_shared.apply_pending_active_pane_width`
    -- `converge_active_pane_width` reports no could-not-converge warning
    here (tmux's `resize()` never reports `achieved_percent`, so the loop
    stops after one attempt), so the per-attempt clamp warning on that
    attempt's own `resize_warning` is what must survive the tuple unpack
    in `apply_pending_active_pane_width`."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    class NarrowWindowBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def _resize_context(self, request: dict) -> dict:
            return {"ok": True, "window_width": 40, "other_pane_count": 1}

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": NarrowWindowBackend, "iterm2": OtherBackend},
    )

    worker = do_spawn(
        registry, tmp_path, active_pane_width=90
    )  # fixture default backend is tmux

    assert worker["state"] == WorkerState.READY.value
    assert "clamped" in worker["resize_warning"]


class FramingBackend(OtherBackend):
    """iterm2-registered backend that also answers `resize`/`get_window_
    frame`/`set_window_frame` with realistic values, for exercising the
    spawn-time convergence + frame save/restore path end to end."""

    instances: ClassVar[list[Any]] = []
    frame: ClassVar[dict] = {
        "ok": True,
        "x": 1.0,
        "y": 2.0,
        "width": 300.0,
        "height": 400.0,
    }
    achieved_percent: int = 68

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        operation = request["operation"]
        if operation == "spawn":
            return self._spawn(request)
        if operation == "verify":
            return self._verify(request)
        if operation == "get_window_frame":
            return dict(type(self).frame)
        if operation == "resize":
            return {"ok": True, "achieved_percent": type(self).achieved_percent}
        return {"ok": True}


def test_spawn_saves_and_restores_the_window_frame_around_the_iterm2_convergence_loop(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    monkeypatch.setattr(spawn_module, "detect_backend", lambda: "iterm2")
    FramingBackend.instances = []
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": RecordingBackend, "iterm2": FramingBackend},
    )

    worker = do_spawn(registry, tmp_path, active_pane_width=70)

    assert worker["state"] == WorkerState.READY.value
    assert (
        "resize_warning" not in worker
    )  # achieved_percent=68 is within tolerance of 70
    calls = [call for instance in FramingBackend.instances for call in instance.calls]
    ops = [call["operation"] for call in calls]
    assert (
        ops.index("spawn")
        < ops.index("get_window_frame")
        < ops.index("resize")
        < ops.index("set_window_frame")
    )
    set_call = calls[ops.index("set_window_frame")]
    assert set_call == {
        "operation": "set_window_frame",
        "window_ref": FramingBackend.window_ref,
        "x": 1.0,
        "y": 2.0,
        "width": 300.0,
        "height": 400.0,
    }


def test_spawn_reports_a_resize_warning_when_the_iterm2_convergence_loop_never_reaches_tolerance(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    monkeypatch.setattr(spawn_module, "detect_backend", lambda: "iterm2")
    FramingBackend.instances = []
    FramingBackend.achieved_percent = 10  # never converges toward 70

    class LocalFramingBackend(FramingBackend):
        instances: ClassVar[list[Any]] = []
        achieved_percent = 10

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {
            "tmux": RecordingBackend,
            "iterm2": LocalFramingBackend,
        },
    )

    worker = do_spawn(registry, tmp_path, active_pane_width=70)

    assert (
        worker["state"] == WorkerState.READY.value
    )  # a resize_warning never fails the spawn
    assert "70%" in worker["resize_warning"]
    resize_calls = [
        call
        for instance in LocalFramingBackend.instances
        for call in instance.calls
        if call["operation"] == "resize"
    ]
    assert len(resize_calls) == MAX_RESIZE_ATTEMPTS


def test_spawn_survives_a_convergence_resize_backend_error_and_reports_a_warning(
    tmp_path: Path, monkeypatch
) -> None:
    """A `BackendError` from the
    convergence loop's `resize()` call must degrade to `resize_warning`, not
    propagate past `_spawn_one()`'s try/finally into `spawn_many()`'s
    `except CrossBufferError` -- the pane split already succeeded, so the
    worker must still reach READY with its `pane_ref` recorded, exactly like
    the existing equalize_warning/tolerance-never-reached warn-don't-fail
    paths above."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    monkeypatch.setattr(spawn_module, "detect_backend", lambda: "iterm2")

    class ResizeBlowsUpBackend(FramingBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            if request["operation"] == "resize":
                self.calls.append(dict(request))
                raise BackendError("iterm2 resize blew up")
            return super().run(request)

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": RecordingBackend, "iterm2": ResizeBlowsUpBackend},
    )

    result = spawn_many(registry, [make_spec(tmp_path)], parent_session_id="parent-1")

    assert result["failed"] == []
    (entry,) = result["succeeded"]
    worker = entry["worker"]
    assert worker["state"] == WorkerState.READY.value
    assert worker["pane_ref"] is not None
    assert worker["resize_warning"] == "iterm2 resize blew up"
    ops = [
        call["operation"]
        for instance in ResizeBlowsUpBackend.instances
        for call in instance.calls
    ]
    assert "set_window_frame" in ops  # frame restore still runs despite the raise


def test_spawn_survives_a_malformed_window_frame_response_without_failing_the_spawn(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    monkeypatch.setattr(spawn_module, "detect_backend", lambda: "iterm2")

    class NoFrameBackend(OtherBackend):
        instances: ClassVar[list[Any]] = []

        def run(self, request: dict) -> dict:
            self.calls.append(dict(request))
            operation = request["operation"]
            if operation == "spawn":
                return self._spawn(request)
            if operation == "verify":
                return self._verify(request)
            if operation == "get_window_frame":
                return {"ok": True}  # missing x/y/width/height -> BackendError
            if operation == "resize":
                return {"ok": True, "achieved_percent": 68}
            return {"ok": True}

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": RecordingBackend, "iterm2": NoFrameBackend},
    )

    worker = do_spawn(registry, tmp_path, active_pane_width=70)

    assert worker["state"] == WorkerState.READY.value
    ops = [
        call["operation"]
        for instance in NoFrameBackend.instances
        for call in instance.calls
    ]
    assert "set_window_frame" not in ops


# --- callable registry resolver (sharded registry) --------


def test_spawn_many_accepts_a_registry_resolver_and_routes_specs_by_shard(
    tmp_path: Path,
) -> None:
    """`spawn_many()`'s `Registry | Callable[[SpawnSpec], Registry]`
    contract: a resolver can route different elements of one batch to
    different shard files (e.g. a ROOT spec vs. a nested spec inheriting a
    different parent's shard). Distinct `role`s (rather than both defaulting
    to "review") keep the two elements' names from colliding -- each lands
    in its own empty shard and would otherwise independently compute
    ordinal=1, which Layer 1's name-uniqueness check now correctly rejects
    (this test is about resolver routing, not about that check, which has
    its own dedicated tests)."""
    root_registry = Registry(tmp_path / "root-shard.json")
    child_registry = Registry(tmp_path / "child-shard.json")

    def resolve(spec: SpawnSpec) -> Registry:
        return child_registry if spec.parent_worker else root_registry

    result = spawn_many(
        resolve,
        [
            make_spec(tmp_path, role="root-role"),
            make_spec(tmp_path, role="child-role", parent_worker="ccw-manual-parent"),
        ],
        parent_session_id="parent-1",
    )

    assert len(result["succeeded"]) == 2
    (root_worker,) = root_registry.list()
    (child_worker,) = child_registry.list()
    assert root_worker.parent_worker_name is None
    assert child_worker.parent_worker_name == "ccw-manual-parent"


def test_spawn_many_purges_each_distinct_resolved_shard_exactly_once(
    tmp_path: Path, monkeypatch
) -> None:
    shard_a = Registry(tmp_path / "a.json")
    shard_b = Registry(tmp_path / "b.json")
    purge_calls: list[Path] = []
    original_purge = Registry.purge

    def counting_purge(self: Registry, **kwargs):
        purge_calls.append(self.path)
        return original_purge(self, **kwargs)

    monkeypatch.setattr(Registry, "purge", counting_purge)

    # Distinct `role`s keep the three elements' names from colliding across
    # shard_a/shard_b -- this test is only about purge call counting (see
    # the batch name-uniqueness check's own dedicated tests for that check
    # itself).
    specs = [
        make_spec(tmp_path, parent_worker=None, role="role-a1"),
        make_spec(tmp_path, parent_worker=None, role="role-a2"),
        make_spec(tmp_path, parent_worker=None, role="role-b"),
    ]
    # First two specs resolve to shard_a, third to shard_b.
    resolution = iter([shard_a, shard_a, shard_b])

    def resolve(spec: SpawnSpec) -> Registry:
        return next(resolution)

    spawn_many(resolve, specs, parent_session_id="parent-1")

    assert purge_calls == [shard_a.path, shard_b.path]


def test_spawn_many_with_a_plain_registry_purges_exactly_once_before_the_loop(
    tmp_path: Path, monkeypatch
) -> None:
    """Backward-compat guarantee: passing a single `Registry` (the original
    contract, and every existing test in this file) must purge exactly
    once for the whole batch, same as before this resolver was added."""
    registry = Registry(tmp_path / "registry.json")
    purge_calls: list[Path] = []
    original_purge = Registry.purge

    def counting_purge(self: Registry, **kwargs):
        purge_calls.append(self.path)
        return original_purge(self, **kwargs)

    monkeypatch.setattr(Registry, "purge", counting_purge)

    spawn_many(
        registry,
        [make_spec(tmp_path), make_spec(tmp_path)],
        parent_session_id="parent-1",
    )

    assert purge_calls == [registry.path]


def test_spawn_many_purge_failure_on_a_later_shard_prevents_any_commits_in_the_batch(
    tmp_path: Path, monkeypatch
) -> None:
    """`spawn_many()` resolves every spec's shard and purges each distinct
    one up front, so a purge failure anywhere is a true batch-wide
    precondition: nothing in the batch is spawned at all -- a purge
    interleaved per-element inside the commit loop would instead let a
    purge failure on a *later* shard propagate out only after *earlier*
    shards' specs had already committed workers, discarding their
    `succeeded` entries from the return value even though those workers
    were really spawned (live pane, registered in their shard)."""
    shard_a = Registry(tmp_path / "a.json")
    shard_b = Registry(tmp_path / "b.json")
    original_purge = Registry.purge

    def flaky_purge(self: Registry, **kwargs):
        if self.path == shard_b.path:
            raise RegistryIOError("simulated purge failure")
        return original_purge(self, **kwargs)

    monkeypatch.setattr(Registry, "purge", flaky_purge)

    # First spec resolves to shard_a; second resolves to shard_b, whose
    # purge fails.
    resolution = iter([shard_a, shard_b])

    def resolve(spec: SpawnSpec) -> Registry:
        return next(resolution)

    with pytest.raises(RegistryIOError):
        spawn_many(
            resolve,
            [make_spec(tmp_path), make_spec(tmp_path)],
            parent_session_id="parent-1",
        )

    # Neither shard received a worker: the precondition failed before any
    # element was processed.
    assert shard_a.list() == []
    assert shard_b.list() == []


def test_spawn_many_layer1_read_failure_is_also_a_batch_wide_precondition(
    tmp_path: Path, monkeypatch
) -> None:
    """Same posture as the purge-failure regression above, for card
    C1009C1E's own new precondition step: `_validate_batch_name_
    uniqueness()`'s `Registry.read_state()` call is not wrapped in a
    per-element `try`/`except CrossBufferError` (it runs entirely before
    the element loop starts), so a `RegistryIOError` from it propagates
    out of `spawn_many()` uncaught -- nothing in the batch is spawned at
    all, the same as a purge failure."""
    registry = Registry(tmp_path / "registry.json")

    def flaky_read_state(self: Registry):
        raise RegistryIOError("simulated read_state failure")

    monkeypatch.setattr(Registry, "read_state", flaky_read_state)

    with pytest.raises(RegistryIOError):
        spawn_many(
            registry,
            [make_spec(tmp_path)],
            parent_session_id="parent-1",
        )

    # `Registry.list()` goes through `_read_unlocked()` directly, not
    # `read_state()` -- unaffected by the patch above, so no unpatch
    # needed before this assertion.
    assert registry.list() == []

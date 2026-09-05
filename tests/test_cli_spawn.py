import io
import json
from datetime import UTC, datetime, timedelta
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

import sukuna.domain.service.spawn_preflight as spawn_preflight_module
import sukuna.infrastructure.terminal.operations as operations_module
import sukuna.usecase.spawn as spawn_module
from sukuna.cli import SPAWN_PARTIAL_FAILURE_EXIT_CODE, main
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.domain.service.spawn_placement import worker_name
from sukuna.errors import (
    BackendError,
    ConflictError,
    RegistryDocumentError,
    RegistryIOError,
)
from sukuna.infrastructure.registry import Registry


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch):
    # Spawn (including its anchor `verify` call) and close both dispatch
    # through `infrastructure.terminal.operations`, which is the single
    # place `BACKENDS` needs patching now.
    install_backends(monkeypatch, {"tmux": RecordingBackend, "iterm2": OtherBackend})
    patch_pane_resolution(monkeypatch, spawn_module)
    yield


@pytest.fixture(autouse=True)
def _isolated_settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`preflight_spawn_specs()` calls `load_active_pane_width()`, which
    falls back to `$XDG_STATE_HOME` (or `~/Library/Application Support`) for
    `setting.toml` -- isolate it to a per-test scratch directory so a real
    settings file on the machine running these tests can never leak into a
    spawn CLI test (same reasoning as mocking the terminal backends instead
    of hitting a real one: real-filesystem/real-terminal dependence makes
    tests flaky and environment-dependent)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


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
    after the `spawn` call."""
    return [
        call
        for instance in RecordingBackend.instances
        for call in instance.calls
        if call["operation"] == "resize"
    ]


_UNSPECIFIED = object()


def spawn_spec(
    repo: Path,
    *,
    goal: str | None = None,
    parent_worker: str | None = None,
    active_pane_width: object = _UNSPECIFIED,
    model: object = _UNSPECIFIED,
) -> dict[str, object]:
    """`active_pane_width` and `model` default to `_UNSPECIFIED` (the key is
    left off the spec entirely) so callers can distinguish "key absent" from
    "key present with a JSON `null` value" by passing e.g.
    `active_pane_width=None` explicitly -- both resolve to the same
    default."""
    spec: dict[str, object] = {
        "role": "review",
        "repo": str(repo),
        "worktree": str(repo),
    }
    if goal is not None:
        spec["goal"] = goal
    if parent_worker is not None:
        spec["parent_worker"] = parent_worker
    if active_pane_width is not _UNSPECIFIED:
        spec["active_pane_width"] = active_pane_width
    if model is not _UNSPECIFIED:
        spec["model"] = model
    return spec


def run_spawn(
    registry_path: Path, specs: list[dict], monkeypatch, capsys
) -> tuple[int, dict]:
    """`handle_spawn()` reads `$ANTHROPIC_API_KEY` from this process's real
    environment to opt in to model-catalog validation -- clear it here so
    every CLI-level spawn test stays hermetic regardless of what happens to
    be set on the machine actually running the suite (same flakiness class
    the owner ruled against for tmux: real external state must never leak
    into a test's pass/fail)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(specs)))
    exit_code = main(["--registry", str(registry_path), "spawn"])
    payload = json.loads(capsys.readouterr().out)
    return exit_code, payload


def test_failed_worker_does_not_corrupt_the_next_anchor(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    failed = make_worker(tmp_path, suffix="a")  # no pane_ref: never got past STARTING
    registry.add(failed)
    registry.transition(failed.name, WorkerState.FAILED)

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == 0
    assert payload["failed"] == []
    call = last_spawn_call()
    assert call["anchor_pane_ref"] == "orchestrator-pane"
    assert call["split_direction"] == "horizontal"


def test_backend_mismatch_is_a_runtime_failure_not_a_batch_wide_rejection(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Backend mismatch depends on registry state, so it is a runtime
    check (best-effort per element), not a pre-flight one -- a lone
    failing element still yields the batch-partial-failure envelope and
    exit code, not the whole-batch-rejected one."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    # Environment resolves to "tmux" (fixture default) but this worker's
    # pane looks like an iTerm2 session id.
    live = make_worker(tmp_path, suffix="a", pane_ref=ITERM2_PANE.format(0))
    registry.add(live)
    registry.transition(live.name, WorkerState.READY)

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == SPAWN_PARTIAL_FAILURE_EXIT_CODE
    assert payload["succeeded"] == []
    assert len(payload["failed"]) == 1
    assert payload["failed"][0]["index"] == 0
    assert RecordingBackend.instances == []
    assert OtherBackend.instances == []


def test_spawn_records_the_calling_session_as_parent(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "orchestrator-session-id")

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == 0
    registry = Registry(registry_path)
    (worker,) = registry.list()
    assert worker.parent_session_id == "orchestrator-session-id"
    assert payload["succeeded"][0]["worker"]["name"] == worker.name


def test_spawn_records_the_given_parent_worker_name(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")

    exit_code, _ = run_spawn(
        registry_path,
        [spawn_spec(tmp_path, parent_worker="ccw-x-review-orchestrator-1")],
        monkeypatch,
        capsys,
    )

    assert exit_code == 0
    registry = Registry(registry_path)
    (worker,) = registry.list()
    assert worker.parent_worker_name == "ccw-x-review-orchestrator-1"


def test_spawn_leaves_parent_worker_name_unset_by_default(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")

    exit_code, _ = run_spawn(registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys)

    assert exit_code == 0
    registry = Registry(registry_path)
    (worker,) = registry.list()
    assert worker.parent_worker_name is None


def test_spawn_stores_goal_but_never_forwards_it_to_the_command(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")

    exit_code, _ = run_spawn(
        registry_path,
        [spawn_spec(tmp_path, goal="investigate the flaky test")],
        monkeypatch,
        capsys,
    )

    assert exit_code == 0
    registry = Registry(registry_path)
    (worker,) = registry.list()
    assert worker.goal == "investigate the flaky test"
    # `tmp_path` (used as the repo dir) is itself named after this test
    # function, so it can legitimately contain the substring "goal" via
    # `worker_name()`'s repo slug -- only the goal *value* must be absent.
    command = last_spawn_call()["command"]
    assert "investigate the flaky test" not in command
    assert "--goal" not in command


def test_spawn_stores_model_and_forwards_it_to_the_command(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")

    exit_code, _ = run_spawn(
        registry_path, [spawn_spec(tmp_path, model="sonnet-5")], monkeypatch, capsys
    )

    assert exit_code == 0
    registry = Registry(registry_path)
    (worker,) = registry.list()
    assert worker.model == "sonnet-5"
    command = last_spawn_call()["command"]
    assert "--model sonnet-5" in command


def test_spawn_leaves_model_unset_by_default(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")

    exit_code, _ = run_spawn(registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys)

    assert exit_code == 0
    registry = Registry(registry_path)
    (worker,) = registry.list()
    assert worker.model is None
    command = last_spawn_call()["command"]
    assert "--model" not in command


def test_spawn_explicit_null_model_is_treated_as_unset(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")

    exit_code, _ = run_spawn(
        registry_path, [spawn_spec(tmp_path, model=None)], monkeypatch, capsys
    )

    assert exit_code == 0
    registry = Registry(registry_path)
    (worker,) = registry.list()
    assert worker.model is None
    command = last_spawn_call()["command"]
    assert "--model" not in command


def test_spawn_preflight_rejects_a_non_string_model(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path, model=123)], monkeypatch, capsys
    )

    assert exit_code == 2
    assert payload["ok"] is False
    assert RecordingBackend.instances == []
    assert registry.list() == []


@pytest.mark.parametrize("empty_model", ["", "   "])
def test_spawn_preflight_rejects_an_empty_model(
    tmp_path: Path, monkeypatch, capsys, empty_model: str
) -> None:
    """Empty/whitespace-only `model` — typically a caller's unfilled
    template variable — is rejected statically before any pane operation,
    since it would otherwise reach the spawn command as `claude --model
    ''` whenever no `$ANTHROPIC_API_KEY` catalog check runs. A narrow
    exception to the free-string pass-through; broader lexical validation
    stays out."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path, model=empty_model)], monkeypatch, capsys
    )

    assert exit_code == 2
    assert payload["ok"] is False
    assert RecordingBackend.instances == []
    assert registry.list() == []


def test_spawn_without_a_claude_session_id_leaves_parent_unset(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    exit_code, _ = run_spawn(registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys)

    assert exit_code == 0
    registry = Registry(registry_path)
    (worker,) = registry.list()
    assert worker.parent_session_id is None


def test_spawn_succeeds_with_a_non_ascii_repo_directory_name(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`worker_name()` sanitizes non-ASCII repo slugs (and non-UUID session
    tokens) down to ASCII, so spawn must succeed end-to-end rather than
    fail at `worker_command()`'s `validate_name()` after `registry.add()`
    already created a `pane_ref`-less record (card: worker_name() ASCII
    sanitize)."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    repo = tmp_path / "実験リポ"
    repo.mkdir()

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(repo)], monkeypatch, capsys
    )

    assert exit_code == 0
    assert payload["failed"] == []
    name = payload["succeeded"][0]["worker"]["name"]
    assert name == "ccw-repo-parent-review-1"
    registry = Registry(registry_path)
    (worker,) = registry.list()
    assert worker.name == name


def test_spawn_rejects_a_malformed_parent_worker_name(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")

    exit_code, payload = run_spawn(
        registry_path,
        [spawn_spec(tmp_path, parent_worker="Not Valid!")],
        monkeypatch,
        capsys,
    )

    assert exit_code == 2
    assert payload["ok"] is False
    assert RecordingBackend.instances == []
    assert OtherBackend.instances == []
    registry = Registry(registry_path)
    assert registry.list() == []


def test_close_dispatches_by_the_pane_refs_own_shape(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, suffix="a", pane_ref=TMUX_PANE.format(1))
    registry.add(worker)
    for state in (
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
    ):
        registry.transition(worker.name, state)

    exit_code = main(
        ["--registry", str(registry_path), "close", "--worker", worker.name]
    )

    assert exit_code == 0
    assert RecordingBackend.instances[-1].calls[-1] == {
        "operation": "close",
        "pane_ref": TMUX_PANE.format(1),
    }
    assert OtherBackend.instances == []


def test_spawn_fails_with_conflict_when_the_record_changes_between_add_and_replace(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Simulates a separate process mutating the worker (e.g. via
    `registry.transition()`) in the window between this spawn's
    `registry.add()` and its final `registry.replace()`; the backend call
    is the only hook available in-process, so the race is injected there."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
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

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == SPAWN_PARTIAL_FAILURE_EXIT_CODE
    (worker,) = registry.list()
    assert worker.state is WorkerState.FAILED
    assert worker.pane_ref == TMUX_PANE.format(1)
    assert worker.window_ref == "win-1"
    (entry,) = payload["failed"]
    assert entry["error"]["code"] == ConflictError.code
    assert entry["worker_name"] == worker.name


def test_spawn_conflict_compensation_failure_preserves_the_conflict_error_code(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """CLI-layer twin of the same-named test in test_usecase_spawn.py: the
    same race as
    test_spawn_fails_with_conflict_when_the_record_changes_between_add_and_replace
    above, plus a registry failure in the compensating
    `registry.mutate()` (re-attaching the pane to the conflicting record).
    The JSON `error.code` reported through `main()` must stay `CONFLICT`,
    not swap for the mutate's own failure code."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
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

    def flaky_mutate(self: Registry, name: str, mutation, **kwargs: Any):
        calls["n"] += 1
        # Call 1 = the racing backend's own `registry.transition(...,
        # FAILED)` above (itself implemented via `mutate()`) -- must
        # succeed so the race (and thus the ConflictError) actually
        # happens. Call 2 = the compensating `registry.mutate()` that
        # re-attaches the pane to the conflicting record -- fail only
        # this one.
        if calls["n"] == 2:
            raise RegistryIOError("simulated write failure")
        return original_mutate(self, name, mutation, **kwargs)

    monkeypatch.setattr(Registry, "mutate", flaky_mutate)

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == SPAWN_PARTIAL_FAILURE_EXIT_CODE
    (entry,) = payload["failed"]
    assert entry["error"]["code"] == ConflictError.code
    assert "simulated write failure" in entry["error"]["message"]
    (worker,) = registry.list()
    assert worker.state is WorkerState.FAILED
    # The compensating mutate() never ran, so the pane was never
    # re-attached to the record.
    assert worker.pane_ref is None


def test_spawn_failed_transition_failure_preserves_the_original_error_code(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """CLI-layer twin of the same-named test in test_usecase_spawn.py: when
    the runtime failure's own compensating
    `registry.transition(..., FAILED)` call itself hits a registry
    failure, the JSON `error.code` reported through `main()` must stay the
    *original* failure's code (`BACKEND_ERROR` here, from a broken verify
    backend) rather than getting swapped for the transition's own failure
    code."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
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

    def flaky_transition(self: Registry, name: str, target: WorkerState, **kwargs: Any):
        raise RegistryIOError("simulated write failure")

    monkeypatch.setattr(Registry, "transition", flaky_transition)

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == SPAWN_PARTIAL_FAILURE_EXIT_CODE
    (entry,) = payload["failed"]
    assert entry["error"]["code"] == BackendError.code
    assert "verify unavailable" in entry["error"]["message"]
    assert "simulated write failure" in entry["error"]["message"]
    # The compensating transition() never ran, so the record is really
    # stuck in STARTING on disk.
    (worker,) = registry.list()
    assert worker.state is WorkerState.STARTING
    assert entry["worker_name"] == worker.name


def test_spawn_reports_a_registry_io_failure_as_a_batch_failure_entry(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A disk-level `OSError` from `Registry._write_unlocked()` (e.g. ENOSPC)
    must not abort the whole batch as if it were a pre-flight rejection --
    `Registry` converts it to `RegistryIOError` (a `CrossBufferError`), so
    `spawn_many()`'s per-element `except CrossBufferError` catches it just
    like any other runtime failure: the first element's already-spawned
    pane and worker are still reported in `succeeded`, and only the second
    element lands in `failed`."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")

    original_write_unlocked = Registry._write_unlocked
    calls = {"n": 0}

    def flaky_write_unlocked(self: Registry, state) -> None:
        calls["n"] += 1
        # Call 1 = `spawn_many()`'s own automatic-retention `registry.purge()`
        # (always writes once on a fresh/never-swept registry, to stamp
        # `last_swept_at`, even though nothing is old enough to purge yet);
        # call 2 = element 0's `registry.add()`, call 3 = element 0's
        # `registry.replace()` (both succeed); call 4 = element 1's
        # `registry.add()` -- fail here, after the first element is fully
        # committed, to match the reported repro (disk fills up mid-batch).
        if calls["n"] == 4:
            raise OSError(28, "No space left on device")
        original_write_unlocked(self, state)

    monkeypatch.setattr(Registry, "_write_unlocked", flaky_write_unlocked)

    exit_code, payload = run_spawn(
        registry_path,
        [spawn_spec(tmp_path), spawn_spec(tmp_path)],
        monkeypatch,
        capsys,
    )

    assert exit_code == SPAWN_PARTIAL_FAILURE_EXIT_CODE
    assert len(payload["succeeded"]) == 1
    assert payload["succeeded"][0]["index"] == 0
    assert len(payload["failed"]) == 1
    entry = payload["failed"][0]
    assert entry["index"] == 1
    assert entry["error"]["code"] == "REGISTRY_IO_ERROR"
    # The first element's worker is really in the registry, not lost.
    registry = Registry(registry_path)
    (worker,) = registry.list()
    assert worker.name == payload["succeeded"][0]["worker"]["name"]
    assert worker.state is WorkerState.READY


def test_spawn_second_elements_snapshot_read_failure_is_a_batch_failure_entry(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`_spawn_one()`'s registry snapshot read (`registry.list()`, serving
    both the backend-mismatch check and `_resolve_placement()`) must run
    inside that element's own `try` block: a `RegistryIOError` from it --
    e.g. a disk read failure for the *second* element -- would otherwise
    escape `spawn_many()` uncaught, aborting the whole batch loop in
    `main()`'s last-resort handler and losing the first element's
    already-`succeeded` entry.

    (Layer 1 computes every live element's name/ordinal once, up front,
    before the loop starts (`_validate_batch_name_uniqueness()`), so there
    is no per-element `registry.next_ordinal()` lookup left to fail here;
    `registry.list()` is the earliest per-element read a mid-batch I/O
    failure can land on.)"""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")

    original_read_unlocked = Registry._read_unlocked
    calls = {"n": 0}

    def flaky_read_unlocked(self: Registry):
        calls["n"] += 1
        # `Registry._read_unlocked()` is the shared primitive every locked
        # read goes through (`list()`, `read_state()`, `add()`, `replace()`,
        # `purge()`). Per this batch: call 1 = `spawn_many()`'s own
        # automatic-retention `registry.purge()`; call 2 = Layer 1's batch
        # name-uniqueness check reading this (single, both
        # elements resolve here) shard's state once (`registry.
        # read_state()`); call 3 = element 0's `_spawn_one()` snapshot read
        # (`registry.list()`); call 4 = element 0's `registry.add()`; call 5
        # = element 0's `registry.replace()` (all five succeed, element 0
        # fully spawns); call 6 = element 1's `_spawn_one()` snapshot read
        # -- fail here, before `registry.add()` is ever reached for
        # element 1.
        if calls["n"] == 6:
            raise RegistryIOError("simulated read failure")
        return original_read_unlocked(self)

    monkeypatch.setattr(Registry, "_read_unlocked", flaky_read_unlocked)

    exit_code, payload = run_spawn(
        registry_path,
        [spawn_spec(tmp_path), spawn_spec(tmp_path)],
        monkeypatch,
        capsys,
    )

    assert exit_code == SPAWN_PARTIAL_FAILURE_EXIT_CODE
    assert len(payload["succeeded"]) == 1
    assert payload["succeeded"][0]["index"] == 0
    assert len(payload["failed"]) == 1
    entry = payload["failed"][0]
    assert entry["index"] == 1
    assert entry["error"]["code"] == "REGISTRY_IO_ERROR"
    assert "worker_name" not in entry
    # Element 1 never reached `registry.add()`, so only element 0 is on disk.
    registry = Registry(registry_path)
    (worker,) = registry.list()
    assert worker.name == payload["succeeded"][0]["worker"]["name"]
    assert worker.state is WorkerState.READY


def test_spawn_worker_name_lookup_registry_failure_is_not_reraised(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The failed-element path's `registry.get(candidate_name)` call (used
    only to
    decide whether to attach `worker_name` to the `failed` entry) must not
    let a `RegistryIOError` from *itself* propagate out of `spawn_many()` --
    that would abort the batch loop on top of the element's original
    failure and lose the first element's already-`succeeded` entry, even
    though the original failure (a backend error here) was already
    correctly contained to one element."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")

    original_spawn = operations_module.spawn
    calls = {"n": 0}

    def flaky_spawn(*args: Any, **kwargs: Any):
        calls["n"] += 1
        # Call 1 = element 0's backend spawn (succeeds); call 2 = element
        # 1's backend spawn -- fail here with an ordinary backend error, the
        # same kind of failure that already lands `worker_name` in a
        # `failed` entry (see the invalid-backend-response test below) --
        # except this time the `registry.get()` lookup that would supply it
        # is itself broken.
        if calls["n"] == 2:
            raise BackendError("simulated backend spawn failure")
        return original_spawn(*args, **kwargs)

    monkeypatch.setattr(operations_module, "spawn", flaky_spawn)

    def flaky_get(self: Registry, name: str):
        raise RegistryIOError("simulated read failure")

    monkeypatch.setattr(Registry, "get", flaky_get)

    exit_code, payload = run_spawn(
        registry_path,
        [spawn_spec(tmp_path), spawn_spec(tmp_path)],
        monkeypatch,
        capsys,
    )

    assert exit_code == SPAWN_PARTIAL_FAILURE_EXIT_CODE
    assert len(payload["succeeded"]) == 1
    assert payload["succeeded"][0]["index"] == 0
    assert len(payload["failed"]) == 1
    entry = payload["failed"][0]
    assert entry["index"] == 1
    assert entry["error"]["code"] == "BACKEND_ERROR"
    assert "worker_name" not in entry
    # Element 1's worker record is really on disk (add() + FAILED transition
    # both happened) -- the broken `registry.get()` just means spawn_many()
    # couldn't confirm that itself, not that the write never happened.
    registry = Registry(registry_path)
    workers = {worker.name: worker.state for worker in registry.list()}
    assert len(workers) == 2
    assert sorted(workers.values(), key=str) == sorted(
        [WorkerState.READY, WorkerState.FAILED], key=str
    )


def test_spawn_worker_name_lookup_document_error_is_not_reraised(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The same best-effort `registry.get(candidate_name)` lookup covered
    by the test above must also swallow
    `RegistryDocumentError`/`SchemaVersionError` -- registry.json content
    corruption or a schema mismatch, a different failure family from the
    `RegistryIOError` disk failure already covered."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")

    original_spawn = operations_module.spawn
    calls = {"n": 0}

    def flaky_spawn(*args: Any, **kwargs: Any):
        calls["n"] += 1
        if calls["n"] == 2:
            raise BackendError("simulated backend spawn failure")
        return original_spawn(*args, **kwargs)

    monkeypatch.setattr(operations_module, "spawn", flaky_spawn)

    def flaky_get(self: Registry, name: str):
        raise RegistryDocumentError("simulated malformed registry.json")

    monkeypatch.setattr(Registry, "get", flaky_get)

    exit_code, payload = run_spawn(
        registry_path,
        [spawn_spec(tmp_path), spawn_spec(tmp_path)],
        monkeypatch,
        capsys,
    )

    assert exit_code == SPAWN_PARTIAL_FAILURE_EXIT_CODE
    assert len(payload["succeeded"]) == 1
    assert payload["succeeded"][0]["index"] == 0
    assert len(payload["failed"]) == 1
    entry = payload["failed"][0]
    assert entry["index"] == 1
    assert entry["error"]["code"] == "BACKEND_ERROR"
    assert "worker_name" not in entry


def test_spawn_reports_an_invalid_backend_response_as_a_batch_failure_entry(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """When the backend's spawn response fails `SpawnResult` validation,
    the worker is failed (fix 2) and the CLI still surfaces a well-formed
    JSON `failed` entry (not an uncaught pydantic traceback)."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)

    class MalformedSpawnBackend(RecordingBackend):
        instances: ClassVar[list[Any]] = []

        def _spawn(self, request: dict) -> dict:
            return {"ok": True, "pane_ref": "", "window_ref": None}

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": MalformedSpawnBackend, "iterm2": OtherBackend},
    )

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == SPAWN_PARTIAL_FAILURE_EXIT_CODE
    (entry,) = payload["failed"]
    assert entry["error"]["code"] == "BACKEND_ERROR"
    (worker,) = registry.list()
    assert worker.state is WorkerState.FAILED


def test_close_routes_an_iterm2_shaped_pane_to_the_iterm2_backend(
    tmp_path: Path, monkeypatch
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, suffix="a", pane_ref=ITERM2_PANE.format(1))
    registry.add(worker)
    for state in (
        WorkerState.READY,
        WorkerState.BUSY,
        WorkerState.REPORTED,
        WorkerState.ACCEPTED,
    ):
        registry.transition(worker.name, state)

    exit_code = main(
        ["--registry", str(registry_path), "close", "--worker", worker.name]
    )

    assert exit_code == 0
    assert OtherBackend.instances[-1].calls[-1] == {
        "operation": "close",
        "pane_ref": ITERM2_PANE.format(1),
    }
    assert RecordingBackend.instances == []


# -- registry-driven recursive placement: representative CLI coverage,
# the placement matrix itself is exercised in tests/test_usecase_spawn.py --


def test_spawn_nests_under_a_named_parent_worker_with_no_children(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    parent = make_worker(tmp_path, suffix="parent", pane_ref=TMUX_PANE.format(1))
    registry.add(parent)
    registry.transition(parent.name, WorkerState.READY)
    RecordingBackend.world = {"orchestrator-pane", TMUX_PANE.format(1)}

    exit_code, _ = run_spawn(
        registry_path,
        [spawn_spec(tmp_path, parent_worker=parent.name)],
        monkeypatch,
        capsys,
    )

    assert exit_code == 0
    call = last_spawn_call()
    assert call["anchor_pane_ref"] == TMUX_PANE.format(1)
    assert call["split_direction"] == "horizontal"


def test_spawn_falls_back_to_root_when_parent_worker_name_does_not_resolve(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)

    exit_code, _ = run_spawn(
        registry_path,
        [spawn_spec(tmp_path, parent_worker="typo-xxx")],
        monkeypatch,
        capsys,
    )

    assert exit_code == 0
    call = last_spawn_call()
    assert call["anchor_pane_ref"] == "orchestrator-pane"
    assert call["split_direction"] == "horizontal"
    (worker,) = registry.list()
    assert worker.state is WorkerState.READY


# -- batching: stdin JSON array, pre-flight rejection, partial runtime failure --


class _FakeTTYStdin(io.StringIO):
    """A bare `StringIO.isatty()` is always `False`, so exercising the
    TTY-rejection path (a human running `sukuna spawn` directly from a
    terminal, with no pipe) needs a subclass that reports `True` instead --
    same shape as `test_cli_init.py`'s `_FakeTTYStdin`."""

    def isatty(self) -> bool:
        return True


def test_spawn_rejects_an_interactive_terminal_on_stdin(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A human running `sukuna spawn` directly from a TTY (no pipe) must get
    an immediate, explicit rejection instead of silently hanging at
    `sys.stdin.read()` until EOF."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    monkeypatch.setattr("sys.stdin", _FakeTTYStdin(""))

    exit_code = main(["--registry", str(registry_path), "spawn"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    # Distinguishes the TTY guard from the malformed-JSON preflight path
    # below -- both share the same exit code and `ok: False` shape, but
    # only the TTY guard's message mentions piping.
    assert "pipe it in" in payload["message"]
    assert RecordingBackend.instances == []
    assert OtherBackend.instances == []


def test_spawn_requires_a_json_array_on_stdin(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"role": "review"})))

    exit_code = main(["--registry", str(registry_path), "spawn"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert RecordingBackend.instances == []


def test_spawn_rejects_malformed_json_on_stdin(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))

    exit_code = main(["--registry", str(registry_path), "spawn"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_spawn_preflight_rejects_the_whole_batch_on_a_single_bad_element(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """One invalid element (a missing required key) among otherwise-valid
    ones must reject the entire batch before any pane or registry
    operation runs -- not just the bad element."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    specs: list[dict[str, object]] = [
        spawn_spec(tmp_path),
        {"role": "review", "repo": str(tmp_path)},
    ]  # missing "worktree"

    exit_code, payload = run_spawn(registry_path, specs, monkeypatch, capsys)

    assert exit_code == 2
    assert payload["ok"] is False
    assert RecordingBackend.instances == []
    assert OtherBackend.instances == []
    assert registry.list() == []


def test_spawn_preflight_rejects_an_empty_batch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """An empty array must reject the whole batch (exit 2) before any pane
    or registry operation runs, and before `detect_backend()` is even
    reachable -- not silently succeed with empty `succeeded`/`failed`
    arrays and exit 0."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)

    exit_code, payload = run_spawn(registry_path, [], monkeypatch, capsys)

    assert exit_code == 2
    assert payload["ok"] is False
    assert RecordingBackend.instances == []
    assert OtherBackend.instances == []
    assert registry.list() == []


def test_spawn_preflight_rejects_a_nonexistent_repo_directory(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    missing = tmp_path / "does-not-exist"

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(missing)], monkeypatch, capsys
    )

    assert exit_code == 2
    assert payload["ok"] is False
    assert registry.list() == []


def test_spawn_preflight_rejects_an_empty_repo(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`Path("").resolve()` returns the process cwd (an always-existing
    directory), so an empty `repo` must be rejected before that resolution
    happens -- otherwise it silently resolves to the orchestrator's own
    cwd instead of failing preflight."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    spec = spawn_spec(tmp_path)
    spec["repo"] = ""

    exit_code, payload = run_spawn(registry_path, [spec], monkeypatch, capsys)

    assert exit_code == 2
    assert payload["ok"] is False
    assert RecordingBackend.instances == []
    assert registry.list() == []


def test_spawn_preflight_rejects_an_empty_worktree(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    spec = spawn_spec(tmp_path)
    spec["worktree"] = ""

    exit_code, payload = run_spawn(registry_path, [spec], monkeypatch, capsys)

    assert exit_code == 2
    assert payload["ok"] is False
    assert RecordingBackend.instances == []
    assert registry.list() == []


def test_spawn_preflight_rejects_a_malformed_role(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    spec = spawn_spec(tmp_path)
    spec["role"] = "Not Valid!"

    exit_code, payload = run_spawn(registry_path, [spec], monkeypatch, capsys)

    assert exit_code == 2
    assert payload["ok"] is False
    assert RecordingBackend.instances == []
    assert registry.list() == []


def test_spawn_preflight_rejects_a_generated_name_that_fails_validate_name(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Defense-in-depth guard (card: worker_name() ASCII sanitize) -- if
    `worker_name()` were to regress and produce a name `validate_name()`
    rejects, preflight must catch it before any registry write (batch
    rejected, zero pane/registry operations), not let it surface only at
    `worker_command()` time after `registry.add()` already ran. The error
    message must carry the generated name itself so the cause (not just
    "worker name must use lowercase letters, numbers, and hyphens") is
    diagnosable."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    monkeypatch.setattr(
        spawn_preflight_module, "worker_name", lambda *a, **k: "Not Valid!"
    )

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == 2
    assert payload["ok"] is False
    assert "Not Valid!" in payload["message"]
    assert RecordingBackend.instances == []
    assert OtherBackend.instances == []
    assert registry.list() == []


def test_spawn_preflight_rejects_an_unknown_key(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A typo'd optional key (e.g. "parent-worker" instead of
    "parent_worker") must reject the whole batch rather than silently
    resolving to the key's absence -- typos in optional keys would
    otherwise drop intent (a missing parent link, a missing goal) without
    any signal."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    spec = spawn_spec(tmp_path)
    spec["parent-worker"] = "ccw-x-review-abc123"

    exit_code, payload = run_spawn(registry_path, [spec], monkeypatch, capsys)

    assert exit_code == 2
    assert payload["ok"] is False
    assert "parent-worker" in payload["message"]
    assert RecordingBackend.instances == []
    assert registry.list() == []


def test_spawn_preflight_rejects_an_out_of_range_active_pane_width(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)

    exit_code, payload = run_spawn(
        registry_path,
        [spawn_spec(tmp_path, active_pane_width=100)],
        monkeypatch,
        capsys,
    )

    assert exit_code == 2
    assert payload["ok"] is False
    assert RecordingBackend.instances == []
    assert registry.list() == []


def test_spawn_preflight_rejects_a_non_integer_active_pane_width(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    spec = spawn_spec(tmp_path)
    spec["active_pane_width"] = "70"

    exit_code, payload = run_spawn(registry_path, [spec], monkeypatch, capsys)

    assert exit_code == 2
    assert payload["ok"] is False
    assert registry.list() == []


def test_spawn_forwards_the_explicit_active_pane_width_to_the_backend(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")

    exit_code, _payload = run_spawn(
        registry_path, [spawn_spec(tmp_path, active_pane_width=70)], monkeypatch, capsys
    )

    assert exit_code == 0
    assert "active_pane_width" not in last_spawn_call()
    (resize_call,) = resize_calls()
    assert resize_call["percent"] == 70


def test_spawn_falls_back_to_the_settings_file_default_when_unspecified(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    monkeypatch.setattr(spawn_module, "load_active_pane_width", lambda: 65)

    exit_code, _payload = run_spawn(
        registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == 0
    assert "active_pane_width" not in last_spawn_call()
    (resize_call,) = resize_calls()
    assert resize_call["percent"] == 65


def test_spawn_explicit_active_pane_width_overrides_the_settings_file_default(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    monkeypatch.setattr(spawn_module, "load_active_pane_width", lambda: 65)

    exit_code, _payload = run_spawn(
        registry_path, [spawn_spec(tmp_path, active_pane_width=70)], monkeypatch, capsys
    )

    assert exit_code == 0
    assert "active_pane_width" not in last_spawn_call()
    (resize_call,) = resize_calls()
    assert resize_call["percent"] == 70


def test_spawn_explicit_null_active_pane_width_falls_back_to_the_settings_file_default(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """An explicit JSON `null` here does NOT disable resizing -- it
    resolves to the same settings-file default as an absent key."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    monkeypatch.setattr(spawn_module, "load_active_pane_width", lambda: 65)

    exit_code, _payload = run_spawn(
        registry_path,
        [spawn_spec(tmp_path, active_pane_width=None)],
        monkeypatch,
        capsys,
    )

    assert exit_code == 0
    assert "active_pane_width" not in last_spawn_call()
    (resize_call,) = resize_calls()
    assert resize_call["percent"] == 65


def test_spawn_batch_of_two_succeeds_and_reports_both_workers(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path), spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == 0
    assert payload["failed"] == []
    assert [entry["index"] for entry in payload["succeeded"]] == [0, 1]
    assert len(registry.list()) == 2


def test_spawn_batch_partial_runtime_failure_reports_nonzero_exit_and_both_arrays(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A runtime failure on one element (a malformed backend spawn response,
    here injected only on the first backend `spawn` call) still lets the
    rest of the batch run to completion (best-effort), and the process exit
    code reflects the partial failure -- distinct from the whole-batch
    pre-flight-rejection exit code (2)."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
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

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path), spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == SPAWN_PARTIAL_FAILURE_EXIT_CODE
    assert [entry["index"] for entry in payload["failed"]] == [0]
    assert [entry["index"] for entry in payload["succeeded"]] == [1]
    assert len(registry.list()) == 2


# -- automatic retention thinning --


def test_spawn_automatically_purges_old_closed_records(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    ancient = make_worker(tmp_path, suffix="a")
    ancient.state = WorkerState.CLOSED
    ancient.pane_ref = None
    ancient.updated_at = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
    registry.add(ancient)

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == 0
    names = {worker.name for worker in registry.list()}
    assert ancient.name not in names
    assert payload["succeeded"][0]["worker"]["name"] in names


def test_spawn_after_purge_does_not_collide_with_a_name_already_spoken_for(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """End-to-end repro: delete then respawn must not collide on a name.
    Seed a v2-era-shaped session (no ordinal_high_water_marks entries at
    all) with 3 old CLOSED records under
    the same deterministic naming scheme `worker_name()` produces, then
    spawn once more under that session: the automatic purge fires first
    (dropping all 3), but the floor it seeds before dropping them
    (`seed_ordinal_floors_before_purge()`) must still make the new spawn
    land on ordinal 4 -- never colliding with (or reusing) ordinals 1-3."""
    registry_path = tmp_path / "registry.json"
    session_id = "parent-1"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    registry = Registry(registry_path)
    old_names = []
    for ordinal in (1, 2, 3):
        old = make_worker(
            tmp_path,
            suffix=None,
            parent_session_id=session_id,
        )
        old.name = worker_name(tmp_path, session_id, "review", ordinal)
        old.state = WorkerState.CLOSED
        old.pane_ref = None
        old.updated_at = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
        registry.add(old)
        old_names.append(old.name)

    exit_code, payload = run_spawn(
        registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys
    )

    assert exit_code == 0
    assert payload["succeeded"][0]["index"] == 0
    new_name = payload["succeeded"][0]["worker"]["name"]
    assert new_name not in old_names
    assert new_name == worker_name(tmp_path, session_id, "review", 4)
    assert {record.name for record in registry.list()} == {new_name}


def test_spawn_purge_is_throttled_to_once_per_utc_day(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A second `sukuna spawn` call the same (fake) UTC day must not re-run
    the sweep -- a just-CLOSED record aged past retention some time between
    the two calls stays put until the throttle clears (a later `spawn`, or
    `reconcile --prune`)."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-1")
    registry = Registry(registry_path)
    # First spawn call stamps `last_swept_at` for "today".
    exit_code, _ = run_spawn(registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys)
    assert exit_code == 0

    ancient = make_worker(tmp_path, suffix="a")
    ancient.state = WorkerState.CLOSED
    ancient.pane_ref = None
    ancient.updated_at = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
    registry.add(ancient)

    exit_code, _ = run_spawn(registry_path, [spawn_spec(tmp_path)], monkeypatch, capsys)

    assert exit_code == 0
    names = {worker.name for worker in registry.list()}
    assert ancient.name in names

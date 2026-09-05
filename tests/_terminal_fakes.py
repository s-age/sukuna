"""Shared terminal-backend fakes and a `WorkerRecord` factory. Sits alongside
`_boundary_support.py` as a plain importable module rather than a
`conftest.py`, so patching stays opt-in per test file instead of an
autouse fixture reaching every test in the suite.

`RecordingBackend.run()` implements the full spawn/verify/equalize/
pane_heights/resize_context lifecycle shared by the CLI/usecase spawn and
respawn test files, which import and use it directly. Test files whose fake needs a
different response policy (keyed-by-pane-ref canned responses, conditional
raises, etc.) don't subclass it -- they define their own standalone fake
class locally (same `__init__`/`instances` bookkeeping, a distinct `run()`)
since that response policy is real per-test business logic, not duplication
to collapse.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, ClassVar, TypedDict, Unpack

import sukuna.infrastructure.terminal.operations as operations_module
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState

TMUX_PANE = "%{}"
ITERM2_PANE = "AAAAAAAA-0000-0000-0000-{:012d}"

ORCHESTRATOR_PANE = "orchestrator-pane"


class RecordingBackend:
    """A fake backend that never touches a real terminal; records every
    request it was asked to perform, and keeps `world` -- the set of panes
    that currently exist -- so that `verify` sees a realistic membership
    built from prior spawns. Registered under the 'tmux' key by convention
    -- its spawn results use tmux-shaped pane refs so `infer_backend`
    agrees."""

    instances: ClassVar[list[RecordingBackend]] = []
    world: ClassVar[set[str]] = set()
    pane_format: ClassVar[str] = TMUX_PANE
    window_ref: ClassVar[str] = "win-1"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        type(self).instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.world = {ORCHESTRATOR_PANE}

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        operation = request["operation"]
        if operation == "spawn":
            return self._spawn(request)
        if operation == "verify":
            return self._verify(request)
        if operation == "equalize":
            return self._equalize(request)
        if operation == "pane_heights":
            return self._pane_heights(request)
        if operation == "resize_context":
            return self._resize_context(request)
        return {"ok": True}

    def _resize_context(self, request: dict) -> dict:
        """Wide, sibling-free window by default so `operations.py`'s tmux
        clamp orchestration never clamps here."""
        return {"ok": True, "window_width": 1000, "other_pane_count": 0}

    def _spawn(self, request: dict) -> dict:
        index = sum(
            1
            for instance in type(self).instances
            for call in instance.calls
            if call["operation"] == "spawn"
        )
        pane_ref = self.pane_format.format(index)
        type(self).world.add(pane_ref)
        return {"ok": True, "pane_ref": pane_ref, "window_ref": self.window_ref}

    def _verify(self, request: dict) -> dict:
        return {"ok": True, "exists": request["pane_ref"] in type(self).world}

    def _equalize(self, request: dict) -> dict:
        """iTerm2's one-shot wire op (`OtherBackend`'s path). tmux equalize
        goes through `pane_heights` + `set_pane_height` instead."""
        return {"ok": True}

    def _pane_heights(self, request: dict) -> dict:
        """An already-equal column by default: `operations.py` computes the
        target from these and follows up with `set_pane_height` writes
        (answered by the generic `{"ok": True}` fallback)."""
        return {"ok": True, "heights": [24] * len(request["column_pane_refs"])}


class OtherBackend(RecordingBackend):
    """Registered under the 'iterm2' key by convention -- its spawn results
    use UUID-shaped pane refs so `infer_backend` agrees."""

    instances: ClassVar[list[Any]] = []
    world: ClassVar[set[str]] = set()
    pane_format: ClassVar[str] = ITERM2_PANE


def install_backends(
    monkeypatch: Any, mapping: dict[str, type[RecordingBackend]]
) -> None:
    """Reset every distinct backend class in `mapping` and patch
    `operations_module.BACKENDS` to it -- the common core of every file's
    own `_fake_backends` fixture."""
    for backend_cls in dict.fromkeys(mapping.values()):
        backend_cls.reset()
    monkeypatch.setattr(operations_module, "BACKENDS", mapping)


def patch_pane_resolution(
    monkeypatch: Any,
    module: Any,
    *,
    backend: str = "tmux",
    orchestrator_pane: str = ORCHESTRATOR_PANE,
    login_shell: str = "/bin/zsh",
) -> None:
    """Pin `detect_backend()`/`orchestrator_pane_ref()`/`resolve_login_shell()`
    on the given usecase module -- the trio every spawn/respawn fixture
    patches identically, just against a different module (`spawn_module`,
    `respawn_module`, ...). Pinning `resolve_login_shell()` keeps tests
    independent of the real host's `$SHELL`/PATH."""
    monkeypatch.setattr(module, "detect_backend", lambda: backend)
    monkeypatch.setattr(
        module, "orchestrator_pane_ref", lambda backend: orchestrator_pane
    )
    monkeypatch.setattr(module, "resolve_login_shell", lambda: login_shell)


class WorkerOverrides(TypedDict, total=False):
    """Every optional field `make_worker()` and its per-file narrowing
    wrappers (`test_cli_tree.py`, `test_usecase_respawn.py`, etc.) accept.
    Bundled as one `Unpack`ed (PEP 692) kwargs type so the declared
    parameter count of each factory stays under Ruff's `PLR0913`
    threshold."""

    name: str | None
    suffix: str | None
    pane_ref: str | None
    window_ref: str | None
    parent_session_id: str | None
    parent_worker_name: str | None
    goal: str | None
    model: str | None
    managed: bool
    worker_state: WorkerState | None
    worktree: Path | None
    session_log_path: str | None
    session_log_reset_at: str | None


def make_worker(repo: Path, **overrides: Unpack[WorkerOverrides]) -> WorkerRecord:
    """Union factory for the 15 copy-pasted `make_worker()`s. Callers that
    need a fully deterministic name pass `name=`; `suffix` (or a random one)
    only feeds the `ccw-x-review-<suffix>` pattern used by files that don't
    care about the exact name. `worker_state` is applied by direct field
    assignment (bypassing `transition_to()`), matching what every original
    factory did."""
    name = overrides.get("name")
    suffix = overrides.get("suffix")
    worktree = overrides.get("worktree")
    worker = WorkerRecord.create(
        name=name or f"ccw-x-review-{suffix or uuid.uuid4().hex[:6]}",
        repo_root=str(repo),
        worktree=str(worktree if worktree is not None else repo),
        parent_session_id=overrides.get("parent_session_id"),
    )
    worker.goal = overrides.get("goal")
    worker.parent_worker_name = overrides.get("parent_worker_name")
    worker.model = overrides.get("model")
    worker.managed = overrides.get("managed", True)
    worker.pane_ref = overrides.get("pane_ref")
    worker.window_ref = overrides.get("window_ref")
    worker.session_log_path = overrides.get("session_log_path")
    worker.session_log_reset_at = overrides.get("session_log_reset_at")
    worker_state = overrides.get("worker_state")
    if worker_state is not None:
        worker.state = worker_state
    return worker

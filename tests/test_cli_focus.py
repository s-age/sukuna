"""These tests pin the CLI surface of the `sukuna focus` verb; the wire
operation itself (`operation="resize"`, internal to usecase/backend) is
covered by tests/test_usecase_resize.py and tests/test_tmux_backend.py."""

import json
from pathlib import Path
from typing import ClassVar

import pytest
from _terminal_fakes import make_worker as _make_worker

import sukuna.infrastructure.terminal.operations as operations_module
import sukuna.usecase.resize as resize_module
from sukuna.cli import main
from sukuna.domain.entity.worker_record import WorkerRecord
from sukuna.infrastructure.registry import Registry

TMUX_PANE = "%1"


class _FocusFakeBackend:
    """Local fake: only `resize_context`/`resize` matter here, so it skips
    the shared `RecordingBackend`'s spawn/verify/equalize lifecycle."""

    instances: ClassVar[list["_FocusFakeBackend"]] = []

    def __init__(self) -> None:
        self.calls: list[dict] = []
        type(self).instances.append(self)

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        if request["operation"] == "resize_context":
            # Wide, sibling-free window by default so `resize()`'s tmux
            # clamp orchestration never clamps here.
            return {"ok": True, "window_width": 1000, "other_pane_count": 0}
        return {"ok": True}


class TmuxRecordingBackend(_FocusFakeBackend):
    instances: ClassVar[list["_FocusFakeBackend"]] = []


class ItermRecordingBackend(_FocusFakeBackend):
    instances: ClassVar[list["_FocusFakeBackend"]] = []


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch):
    TmuxRecordingBackend.instances = []
    ItermRecordingBackend.instances = []
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": TmuxRecordingBackend, "iterm2": ItermRecordingBackend},
    )
    # Without this, `resize()`'s `should_select_pane` branch reads the real
    # machine-local settings file via `load_should_focus_worker()`, so these
    # tests silently depend on the developer's own `sukuna-cli init` choice
    # instead of pinning the CLI-rename behavior they're named for.
    monkeypatch.setattr(resize_module, "load_should_focus_worker", lambda: False)
    yield


def make_worker(repo: Path, *, pane_ref: str | None) -> WorkerRecord:
    return _make_worker(repo, name="ccw-x-review-a", pane_ref=pane_ref)


def test_focus_dispatches_to_the_resize_use_case_for_a_named_worker(
    tmp_path: Path, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE)
    registry.add(worker)

    exit_code = main(
        ["--registry", str(registry_path), "focus", "--worker", worker.name]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    call = TmuxRecordingBackend.instances[-1].calls[-1]
    assert (
        call["operation"] == "resize"
    )  # internal wire name, unaffected by the CLI rename (card scope)
    assert call["pane_ref"] == TMUX_PANE


def test_the_old_resize_verb_no_longer_exists(tmp_path: Path) -> None:
    """Full switch, no alias."""
    registry_path = tmp_path / "registry.json"
    Registry(registry_path)

    with pytest.raises(SystemExit) as excinfo:
        main(["--registry", str(registry_path), "resize", "--worker", "ccw-x-review-a"])

    assert excinfo.value.code == 2
    assert TmuxRecordingBackend.instances == []
    assert ItermRecordingBackend.instances == []


def test_focus_routes_a_tmux_shaped_worker_to_the_tmux_backend_only(
    tmp_path: Path,
) -> None:
    """`focus` is a single shared command for both backends -- this pins
    that a tmux-shaped worker's routing (`infer_backend`) still resolves
    to the tmux backend, never the iTerm2 one."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE)
    registry.add(worker)

    main(["--registry", str(registry_path), "focus", "--worker", worker.name])

    # A tmux resize is 2 wire calls (a raw `resize_context` query, then the
    # raw `resize` itself), each against a
    # fresh backend instance (`operations.py`'s existing per-call
    # `BACKENDS[backend]()` convention) -- what this test actually pins is
    # that only the tmux backend is ever touched, never iTerm2's.
    ops = [
        call["operation"]
        for instance in TmuxRecordingBackend.instances
        for call in instance.calls
    ]
    assert ops == ["resize_context", "resize"]
    assert ItermRecordingBackend.instances == []

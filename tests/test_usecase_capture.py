from pathlib import Path
from typing import ClassVar

import pytest
from _terminal_fakes import ITERM2_PANE, TMUX_PANE
from _terminal_fakes import make_worker as _make_worker

import sukuna.infrastructure.terminal.operations as operations_module
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.errors import BackendError, ValidationError
from sukuna.infrastructure.registry import Registry
from sukuna.usecase.capture import capture


class _CaptureFakeBackend:
    instances: ClassVar[list["_CaptureFakeBackend"]] = []

    def __init__(self) -> None:
        self.calls: list[dict] = []
        type(self).instances.append(self)

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        return {"ok": True, "exists": True, "content": "some pane output"}


class OtherBackend(_CaptureFakeBackend):
    instances: ClassVar[list["_CaptureFakeBackend"]] = []


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch):
    _CaptureFakeBackend.instances = []
    OtherBackend.instances = []
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _CaptureFakeBackend, "iterm2": OtherBackend},
    )
    yield


def make_worker(repo: Path, *, pane_ref: str | None, suffix: str) -> WorkerRecord:
    return _make_worker(
        repo, suffix=suffix, pane_ref=pane_ref, parent_session_id="parent-1"
    )


def test_capture_dispatches_by_the_pane_refs_own_shape(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)

    result = capture(registry, worker.name)

    assert result["ok"] is True
    assert result["exists"] is True
    assert result["content"] == "some pane output"
    assert _CaptureFakeBackend.instances[-1].calls[-1] == {
        "operation": "capture",
        "pane_ref": TMUX_PANE.format(1),
    }
    assert OtherBackend.instances == []


def test_capture_routes_an_iterm2_shaped_pane_to_the_iterm2_backend(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=ITERM2_PANE.format(1), suffix="a")
    registry.add(worker)

    result = capture(registry, worker.name)

    assert result["exists"] is True
    assert OtherBackend.instances[-1].calls[-1] == {
        "operation": "capture",
        "pane_ref": ITERM2_PANE.format(1),
    }
    assert _CaptureFakeBackend.instances == []


def test_capture_rejects_a_worker_with_no_pane(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=None, suffix="a")
    registry.add(worker)

    with pytest.raises(ValidationError):
        capture(registry, worker.name)

    assert _CaptureFakeBackend.instances == []
    assert OtherBackend.instances == []


def test_capture_rejects_a_closed_worker_without_calling_the_backend(
    tmp_path: Path,
) -> None:
    """close() clears pane_ref on the CLOSED record, so a CLOSED worker
    hits the same `pane_ref is None` guard as any other paneless worker --
    it must never reach the backend."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=None, suffix="a")
    worker.state = WorkerState.CLOSED
    registry.add(worker)

    with pytest.raises(ValidationError):
        capture(registry, worker.name)

    assert _CaptureFakeBackend.instances == []
    assert OtherBackend.instances == []


def test_capture_raises_a_backend_error_for_an_inconsistent_response(
    tmp_path: Path, monkeypatch
) -> None:
    """`exists=True` with `content=None` is a backend contract violation --
    `parse_backend_response` must translate the underlying pydantic
    `ValidationError` into a `BackendError` rather than letting it escape
    as a bare traceback."""
    registry = Registry(tmp_path / "registry.json")
    worker = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    registry.add(worker)

    class MalformedBackend(_CaptureFakeBackend):
        instances: ClassVar[list["_CaptureFakeBackend"]] = []

        def run(self, request: dict) -> dict:
            self.calls.append(dict(request))
            return {"ok": True, "exists": True, "content": None}

    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": MalformedBackend, "iterm2": OtherBackend},
    )

    with pytest.raises(BackendError):
        capture(registry, worker.name)

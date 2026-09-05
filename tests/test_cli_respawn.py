import json
from pathlib import Path

import pytest
from _terminal_fakes import (
    TMUX_PANE,
    RecordingBackend,
    install_backends,
    patch_pane_resolution,
)
from _terminal_fakes import (
    make_worker as _make_worker,
)

import sukuna.usecase.respawn as respawn_module
from sukuna.cli import main
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.errors import NotFoundError, ValidationError
from sukuna.infrastructure.registry import Registry


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch):
    install_backends(
        monkeypatch, {"tmux": RecordingBackend, "iterm2": RecordingBackend}
    )
    patch_pane_resolution(monkeypatch, respawn_module)
    yield


@pytest.fixture(autouse=True)
def _isolated_settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same reasoning as `test_cli_spawn.py`'s fixture of the same name:
    `respawn()` calls `load_active_pane_width()`, which falls back to the
    real host's `setting.toml` unless `$XDG_STATE_HOME` is isolated."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


def make_worker(repo: Path, *, name: str, pane_ref: str | None) -> WorkerRecord:
    return _make_worker(
        repo, name=name, pane_ref=pane_ref, parent_session_id="old-session"
    )


def _reach(registry: Registry, worker: WorkerRecord, states: list[WorkerState]) -> None:
    for state in states:
        registry.transition(worker.name, state)


def test_respawn_command_reopens_a_closed_worker(tmp_path: Path, capsys) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, name="ccw-x-review-a", pane_ref=None)
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

    exit_code = main(
        ["--registry", str(registry_path), "respawn", "--worker", worker.name]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == WorkerState.READY.value
    assert payload["pane_ref"] is not None
    assert registry.get(worker.name).state is WorkerState.READY


def test_respawn_command_reports_worker_not_found(tmp_path: Path, capsys) -> None:
    registry_path = tmp_path / "registry.json"
    Registry(registry_path)

    exit_code = main(
        ["--registry", str(registry_path), "respawn", "--worker", "ccw-x-review-ghost"]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == NotFoundError.code
    assert RecordingBackend.instances == []


def test_respawn_command_rejects_a_worker_with_an_attached_pane(
    tmp_path: Path, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, name="ccw-x-review-a", pane_ref=TMUX_PANE.format(9))
    registry.add(worker)
    _reach(registry, worker, [WorkerState.READY])

    exit_code = main(
        ["--registry", str(registry_path), "respawn", "--worker", worker.name]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == ValidationError.code
    assert "reconcile" in payload["message"]
    assert RecordingBackend.instances == []


def test_respawn_command_requires_the_worker_flag(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    Registry(registry_path)

    with pytest.raises(SystemExit):
        main(["--registry", str(registry_path), "respawn"])

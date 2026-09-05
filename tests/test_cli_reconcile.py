import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from _terminal_fakes import TMUX_PANE
from _terminal_fakes import make_worker as _make_worker

import sukuna.infrastructure.terminal.operations as operations_module
from sukuna.cli import main
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.infrastructure.registry import Registry


class _ReconcileFakeBackend:
    instances: ClassVar[list["_ReconcileFakeBackend"]] = []
    responses: ClassVar[dict[str, dict]] = {}

    def __init__(self) -> None:
        self.calls: list[dict] = []
        type(self).instances.append(self)

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        return type(self).responses[request["pane_ref"]]


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch):
    _ReconcileFakeBackend.instances = []
    _ReconcileFakeBackend.responses = {}
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": _ReconcileFakeBackend, "iterm2": _ReconcileFakeBackend},
    )
    yield


def make_worker(repo: Path, *, pane_ref: str | None, suffix: str) -> WorkerRecord:
    return _make_worker(
        repo, suffix=suffix, pane_ref=pane_ref, parent_session_id="parent-1"
    )


def test_reconcile_command_reports_the_workers_it_failed(
    tmp_path: Path, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    gone = make_worker(tmp_path, pane_ref=TMUX_PANE.format(1), suffix="a")
    live = make_worker(tmp_path, pane_ref=TMUX_PANE.format(2), suffix="b")
    registry.add(gone)
    registry.add(live)
    registry.transition(gone.name, WorkerState.READY)
    registry.transition(live.name, WorkerState.READY)
    assert gone.pane_ref is not None
    assert live.pane_ref is not None
    _ReconcileFakeBackend.responses[gone.pane_ref] = {"ok": True, "exists": False}
    _ReconcileFakeBackend.responses[live.pane_ref] = {"ok": True, "exists": True}

    exit_code = main(["--registry", str(registry_path), "reconcile"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [worker["name"] for worker in payload["reconciled"]] == [gone.name]
    assert registry.get(gone.name).state is WorkerState.FAILED
    assert registry.get(live.name).state is WorkerState.READY


def test_reconcile_command_with_no_candidates_reports_nothing(
    tmp_path: Path, capsys
) -> None:
    registry_path = tmp_path / "registry.json"
    Registry(registry_path)

    exit_code = main(["--registry", str(registry_path), "reconcile"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"reconciled": [], "pane_cleared": []}
    assert _ReconcileFakeBackend.instances == []


def test_reconcile_without_prune_never_purges(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    ancient = _make_worker(
        tmp_path, suffix="a", pane_ref=None, parent_session_id="parent-1"
    )
    ancient.state = WorkerState.CLOSED
    ancient.updated_at = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
    registry.add(ancient)

    exit_code = main(["--registry", str(registry_path), "reconcile"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "purged" not in payload
    assert {record.name for record in registry.list()} == {ancient.name}


def test_reconcile_prune_purges_old_closed_records(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    ancient = _make_worker(
        tmp_path, suffix="a", pane_ref=None, parent_session_id="parent-1"
    )
    ancient.state = WorkerState.CLOSED
    ancient.updated_at = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
    recent = _make_worker(
        tmp_path, suffix="b", pane_ref=None, parent_session_id="parent-1"
    )
    recent.state = WorkerState.CLOSED
    registry.add(ancient)
    registry.add(recent)

    exit_code = main(["--registry", str(registry_path), "reconcile", "--prune"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [record["name"] for record in payload["purged"]] == [ancient.name]
    assert {record.name for record in registry.list()} == {recent.name}


def test_reconcile_prune_bypasses_the_same_day_throttle(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Two `--prune` calls in the same process, same UTC day: the second
    must still purge a newly-eligible record, unlike `sukuna spawn`'s
    automatic thinning (which throttles to once per day)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    first = _make_worker(
        tmp_path, suffix="a", pane_ref=None, parent_session_id="parent-1"
    )
    first.state = WorkerState.CLOSED
    first.updated_at = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
    registry.add(first)
    assert main(["--registry", str(registry_path), "reconcile", "--prune"]) == 0
    capsys.readouterr()

    second = _make_worker(
        tmp_path, suffix="b", pane_ref=None, parent_session_id="parent-1"
    )
    second.state = WorkerState.CLOSED
    second.updated_at = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
    registry.add(second)

    exit_code = main(["--registry", str(registry_path), "reconcile", "--prune"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [record["name"] for record in payload["purged"]] == [second.name]
    assert registry.list() == []

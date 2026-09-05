import shutil
import subprocess

import pytest

from sukuna.infrastructure import node_runtime


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def _stub_which(monkeypatch: pytest.MonkeyPatch, path: str | None) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: path)


def test_node_executable_returns_shutil_which_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_which(monkeypatch, "/usr/local/bin/node")

    assert node_runtime.node_executable() == "/usr/local/bin/node"


def test_node_executable_returns_none_when_not_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_which(monkeypatch, None)

    assert node_runtime.node_executable() is None


def test_node_major_version_parses_a_typical_version_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompleted("v24.1.0\n"),
    )

    assert node_runtime.node_major_version("/usr/bin/node") == 24


def test_node_major_version_returns_none_on_command_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_oserror(*args: object, **kwargs: object) -> None:
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    assert node_runtime.node_major_version("/usr/bin/node") is None


def test_node_major_version_returns_none_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="node", timeout=10)

    monkeypatch.setattr(subprocess, "run", raise_timeout)

    assert node_runtime.node_major_version("/usr/bin/node") is None


def test_node_major_version_returns_none_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted("garbage\n"))

    assert node_runtime.node_major_version("/usr/bin/node") is None


def test_node_satisfies_tui_minimum_true_when_new_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_which(monkeypatch, "/usr/bin/node")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted("v24.1.0\n"))

    assert node_runtime.node_satisfies_tui_minimum() is True


def test_node_satisfies_tui_minimum_false_when_too_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_which(monkeypatch, "/usr/bin/node")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted("v20.11.0\n"))

    assert node_runtime.node_satisfies_tui_minimum() is False


def test_node_satisfies_tui_minimum_false_when_node_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_which(monkeypatch, None)

    assert node_runtime.node_satisfies_tui_minimum() is False


def test_node_status_message_reports_ok_when_new_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_which(monkeypatch, "/usr/bin/node")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted("v24.1.0\n"))

    assert node_runtime.node_status_message() == "Node.js v24.1.0 detected (>= 24, OK)"


def test_node_status_message_reports_too_old(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_which(monkeypatch, "/usr/bin/node")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted("v20.11.0\n"))

    assert node_runtime.node_status_message() == (
        "Node.js v20.11.0 detected (< 24, does not satisfy sukuna's TUI requirement)"
    )


def test_node_status_message_reports_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_which(monkeypatch, None)

    assert node_runtime.node_status_message() == "Node.js not found on PATH"

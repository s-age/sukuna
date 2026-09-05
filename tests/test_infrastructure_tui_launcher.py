import contextlib
import importlib.resources
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import sukuna.infrastructure.tui.launcher as launcher_module
from sukuna.errors import BackendError, ValidationError
from sukuna.infrastructure import node_runtime


class _FakeTraversable:
    def __init__(self, is_file: bool) -> None:
        self._is_file = is_file

    def joinpath(self, *_parts: str) -> "_FakeTraversable":
        return self

    def is_file(self) -> bool:
        return self._is_file


class _FakeCompleted:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _stub_files(monkeypatch: pytest.MonkeyPatch, *, is_file: bool) -> None:
    monkeypatch.setattr(
        importlib.resources,
        "files",
        lambda _package: _FakeTraversable(is_file=is_file),
    )


def _stub_as_file_yielding(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    @contextlib.contextmanager
    def fake_as_file(_traversable: object):
        yield path

    monkeypatch.setattr(importlib.resources, "as_file", fake_as_file)


def _stub_enabled_node(monkeypatch: pytest.MonkeyPatch) -> None:
    # `launcher.py` imports these via `from ..node_runtime import ...`, so
    # each name is its own binding in `launcher_module`'s namespace --
    # patching `node_runtime.*` would not reach the copy `launcher.py` calls.
    monkeypatch.setattr(launcher_module, "node_satisfies_tui_minimum", lambda: True)
    monkeypatch.setattr(launcher_module, "node_executable", lambda: "/usr/bin/node")


def test_bundle_present_returns_the_traversables_is_file_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_files(monkeypatch, is_file=True)

    assert launcher_module.bundle_present() is True

    _stub_files(monkeypatch, is_file=False)

    assert launcher_module.bundle_present() is False


def test_launch_tree_tui_raises_validation_error_when_node_is_too_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher_module, "node_satisfies_tui_minimum", lambda: False)
    monkeypatch.setattr(
        launcher_module, "node_status_message", lambda: "Node.js not found on PATH"
    )

    with pytest.raises(ValidationError) as excinfo:
        launcher_module.launch_tree_tui({"groups": {}, "hidden_parent_groups": 0})

    assert "Node.js not found on PATH" in str(excinfo.value)
    assert str(node_runtime.TUI_MIN_NODE_MAJOR) in str(excinfo.value)


def test_launch_tree_tui_raises_backend_error_when_the_bundle_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_enabled_node(monkeypatch)
    monkeypatch.setattr(launcher_module, "bundle_present", lambda: False)

    with pytest.raises(BackendError):
        launcher_module.launch_tree_tui({"groups": {}, "hidden_parent_groups": 0})


def test_launch_tree_tui_invokes_node_with_the_bundle_and_payload_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_enabled_node(monkeypatch)
    monkeypatch.setattr(launcher_module, "bundle_present", lambda: True)
    bundle_path = tmp_path / "tree-tui.mjs"
    bundle_path.write_text("", encoding="utf-8")
    _stub_as_file_yielding(monkeypatch, bundle_path)
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], check: bool) -> _FakeCompleted:
        captured["argv"] = argv
        captured["payload_path_exists_during_run"] = Path(argv[2]).exists()
        with Path(argv[2]).open(encoding="utf-8") as handle:
            captured["payload_contents"] = json.load(handle)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = {"groups": {"session-a": []}, "hidden_parent_groups": 0}
    exit_code = launcher_module.launch_tree_tui(payload)

    assert exit_code == 0
    assert captured["argv"][0] == "/usr/bin/node"
    assert captured["argv"][1] == str(bundle_path)
    assert captured["payload_path_exists_during_run"] is True
    assert captured["payload_contents"] == payload
    assert not Path(captured["argv"][2]).exists()


def test_launch_tree_tui_returns_the_subprocess_returncode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_enabled_node(monkeypatch)
    monkeypatch.setattr(launcher_module, "bundle_present", lambda: True)
    bundle_path = tmp_path / "tree-tui.mjs"
    bundle_path.write_text("", encoding="utf-8")
    _stub_as_file_yielding(monkeypatch, bundle_path)
    monkeypatch.setattr(
        subprocess, "run", lambda argv, check: _FakeCompleted(returncode=7)
    )

    exit_code = launcher_module.launch_tree_tui(
        {"groups": {}, "hidden_parent_groups": 0}
    )

    assert exit_code == 7


def test_launch_tree_tui_wraps_an_oserror_from_subprocess_as_backend_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_enabled_node(monkeypatch)
    monkeypatch.setattr(launcher_module, "bundle_present", lambda: True)
    bundle_path = tmp_path / "tree-tui.mjs"
    bundle_path.write_text("", encoding="utf-8")
    _stub_as_file_yielding(monkeypatch, bundle_path)

    def raise_oserror(argv: list[str], check: bool) -> None:
        raise OSError("node not runnable")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    with pytest.raises(BackendError):
        launcher_module.launch_tree_tui({"groups": {}, "hidden_parent_groups": 0})


# -- state-dir bundle: existence, stamp, install --------------------------


def test_state_dir_bundle_exists_reflects_file_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert launcher_module.state_dir_bundle_exists() is False

    bundle_path = launcher_module._state_dir_bundle_path()
    bundle_path.parent.mkdir(parents=True)
    bundle_path.write_text("", encoding="utf-8")

    assert launcher_module.state_dir_bundle_exists() is True


def test_state_dir_stamp_fingerprint_returns_none_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert launcher_module.state_dir_stamp_fingerprint() is None


def test_state_dir_stamp_fingerprint_returns_none_for_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    stamp_path = launcher_module._state_dir_stamp_path()
    stamp_path.parent.mkdir(parents=True)
    stamp_path.write_text("{not valid json", encoding="utf-8")

    assert launcher_module.state_dir_stamp_fingerprint() is None


def test_state_dir_stamp_fingerprint_returns_none_when_key_missing_or_non_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    stamp_path = launcher_module._state_dir_stamp_path()
    stamp_path.parent.mkdir(parents=True)

    stamp_path.write_text(json.dumps({}), encoding="utf-8")
    assert launcher_module.state_dir_stamp_fingerprint() is None

    stamp_path.write_text(json.dumps({"tui_source_fingerprint": 123}), encoding="utf-8")
    assert launcher_module.state_dir_stamp_fingerprint() is None


def test_state_dir_stamp_fingerprint_returns_the_stored_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    stamp_path = launcher_module._state_dir_stamp_path()
    stamp_path.parent.mkdir(parents=True)
    stamp_path.write_text(
        json.dumps({"tui_source_fingerprint": "abc123"}), encoding="utf-8"
    )

    assert launcher_module.state_dir_stamp_fingerprint() == "abc123"


def test_install_bundle_from_build_writes_the_bundle_and_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    built = tmp_path / "built-tree-tui.mjs"
    built.write_bytes(b"console.log('tui')")

    launcher_module.install_bundle_from_build(built, fingerprint="fp-1")

    assert launcher_module.state_dir_bundle_exists() is True
    assert (
        launcher_module._state_dir_bundle_path().read_bytes() == b"console.log('tui')"
    )
    assert launcher_module.state_dir_stamp_fingerprint() == "fp-1"


# -- launch_tree_tui: state-dir bundle resolution --------------------------


def test_launch_tree_tui_uses_the_state_dir_bundle_when_usable_and_package_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_enabled_node(monkeypatch)
    monkeypatch.setattr(launcher_module, "bundle_present", lambda: False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state_bundle_path = launcher_module._state_dir_bundle_path()
    state_bundle_path.parent.mkdir(parents=True)
    state_bundle_path.write_text("", encoding="utf-8")

    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], check: bool) -> _FakeCompleted:
        captured["argv"] = argv
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = launcher_module.launch_tree_tui(
        {"groups": {}, "hidden_parent_groups": 0}, state_dir_bundle_usable=True
    )

    assert exit_code == 0
    assert captured["argv"][1] == str(state_bundle_path)


def test_launch_tree_tui_raises_with_init_guidance_when_neither_bundle_is_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_enabled_node(monkeypatch)
    monkeypatch.setattr(launcher_module, "bundle_present", lambda: False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    with pytest.raises(BackendError) as excinfo:
        launcher_module.launch_tree_tui(
            {"groups": {}, "hidden_parent_groups": 0}, state_dir_bundle_usable=True
        )

    assert "sukuna-cli init" in str(excinfo.value)


def test_launch_tree_tui_ignores_state_dir_bundle_when_flag_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_enabled_node(monkeypatch)
    monkeypatch.setattr(launcher_module, "bundle_present", lambda: False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state_bundle_path = launcher_module._state_dir_bundle_path()
    state_bundle_path.parent.mkdir(parents=True)
    state_bundle_path.write_text("", encoding="utf-8")

    with pytest.raises(BackendError):
        launcher_module.launch_tree_tui({"groups": {}, "hidden_parent_groups": 0})

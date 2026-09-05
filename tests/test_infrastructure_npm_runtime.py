"""Same shape as test_node_runtime.py -- `shutil.which`/`subprocess.run`
are monkeypatched, real npm is never invoked (see test_npm_smoke.py for
the one place that touches a real binary). `copy_tui_source`/
`embedded_tui_source_fingerprint` are exercised against a stubbed
`importlib.resources.files()` tree instead of the repository's real
`tui/`, so these tests do not depend on `scripts/build_tui.sh` having
been run."""

from __future__ import annotations

import importlib.resources
import shutil
import subprocess
from pathlib import Path

import pytest

from sukuna.errors import BackendError
from sukuna.infrastructure import npm_runtime


class _FakeTraversable:
    def __init__(
        self,
        name: str = "",
        *,
        is_dir: bool = False,
        is_file: bool = False,
        children: list[_FakeTraversable] | None = None,
        content: bytes = b"",
    ) -> None:
        self.name = name
        self._is_dir = is_dir
        self._is_file = is_file
        self._children = children or []
        self._content = content

    def is_dir(self) -> bool:
        return self._is_dir

    def is_file(self) -> bool:
        return self._is_file

    def iterdir(self) -> list[_FakeTraversable]:
        return list(self._children)

    def read_bytes(self) -> bytes:
        return self._content

    def joinpath(self, *parts: str) -> _FakeTraversable:
        node = self
        for part in parts:
            match = next((c for c in node._children if c.name == part), None)
            node = match if match is not None else _FakeTraversable(part)
        return node


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_which(monkeypatch: pytest.MonkeyPatch, path: str | None) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: path)


def _stub_tui_source_tree(
    monkeypatch: pytest.MonkeyPatch, tree: _FakeTraversable | None
) -> None:
    """`tree`, when given, stands in for the `_tui_source` directory
    itself (not its parent) -- `None` simulates an install with no
    embedded `_tui_source` at all (an unresolved joinpath segment)."""
    root_children = [tree] if tree is not None else []
    root = _FakeTraversable("sukuna", is_dir=True, children=root_children)
    monkeypatch.setattr(importlib.resources, "files", lambda _package: root)


def _sample_tree(*, second_file_content: bytes = b"content-b") -> _FakeTraversable:
    return _FakeTraversable(
        "_tui_source",
        is_dir=True,
        children=[
            _FakeTraversable("package.json", is_file=True, content=b"content-a"),
            _FakeTraversable(
                "src",
                is_dir=True,
                children=[
                    _FakeTraversable(
                        "cli.ts", is_file=True, content=second_file_content
                    )
                ],
            ),
        ],
    )


# -- npm detection ------------------------------------------------------


def test_npm_executable_returns_shutil_which_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_which(monkeypatch, "/opt/homebrew/bin/npm")

    assert npm_runtime.npm_executable() == "/opt/homebrew/bin/npm"


def test_npm_available_reflects_executable_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_which(monkeypatch, "/opt/homebrew/bin/npm")
    assert npm_runtime.npm_available() is True

    _stub_which(monkeypatch, None)
    assert npm_runtime.npm_available() is False


def test_npm_status_message_reports_found_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_which(monkeypatch, "/opt/homebrew/bin/npm")

    assert (
        npm_runtime.npm_status_message() == "npm found on PATH: /opt/homebrew/bin/npm"
    )


def test_npm_status_message_reports_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_which(monkeypatch, None)

    assert npm_runtime.npm_status_message() == "npm not found on PATH"


# -- tui_source_available / fingerprint ---------------------------------


def test_tui_source_available_true_when_directory_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_tui_source_tree(monkeypatch, _sample_tree())

    assert npm_runtime.tui_source_available() is True


def test_tui_source_available_false_when_directory_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_tui_source_tree(monkeypatch, None)

    assert npm_runtime.tui_source_available() is False


def test_fingerprint_is_none_when_source_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_tui_source_tree(monkeypatch, None)

    assert npm_runtime.embedded_tui_source_fingerprint() is None


def test_fingerprint_is_stable_for_identical_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_tui_source_tree(monkeypatch, _sample_tree())
    first = npm_runtime.embedded_tui_source_fingerprint()

    _stub_tui_source_tree(monkeypatch, _sample_tree())
    second = npm_runtime.embedded_tui_source_fingerprint()

    assert first is not None
    assert first == second


def test_fingerprint_changes_when_one_byte_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_tui_source_tree(monkeypatch, _sample_tree())
    before = npm_runtime.embedded_tui_source_fingerprint()

    _stub_tui_source_tree(monkeypatch, _sample_tree(second_file_content=b"content-B"))
    after = npm_runtime.embedded_tui_source_fingerprint()

    assert before != after


# -- copy_tui_source ------------------------------------------------------


def test_copy_tui_source_raises_when_source_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_tui_source_tree(monkeypatch, None)

    with pytest.raises(BackendError):
        npm_runtime.copy_tui_source(tmp_path / "dest")


def test_copy_tui_source_raises_when_dest_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_tui_source_tree(monkeypatch, _sample_tree())
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(BackendError):
        npm_runtime.copy_tui_source(dest)


def test_copy_tui_source_recreates_the_tree_under_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_tui_source_tree(monkeypatch, _sample_tree())
    dest = tmp_path / "dest"

    npm_runtime.copy_tui_source(dest)

    assert (dest / "package.json").read_bytes() == b"content-a"
    assert (dest / "src" / "cli.ts").read_bytes() == b"content-b"


# -- run_npm_ci / run_npm_build -------------------------------------------


def test_run_npm_ci_raises_when_npm_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(npm_runtime, "npm_executable", lambda: None)

    with pytest.raises(BackendError):
        npm_runtime.run_npm_ci(tmp_path)


def test_run_npm_ci_wraps_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(npm_runtime, "npm_executable", lambda: "/usr/bin/npm")

    def raise_oserror(*args: object, **kwargs: object) -> None:
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    with pytest.raises(BackendError):
        npm_runtime.run_npm_ci(tmp_path)


def test_run_npm_ci_wraps_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(npm_runtime, "npm_executable", lambda: "/usr/bin/npm")

    def raise_timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="npm", timeout=300)

    monkeypatch.setattr(subprocess, "run", raise_timeout)

    with pytest.raises(BackendError):
        npm_runtime.run_npm_ci(tmp_path)


def test_run_npm_ci_wraps_non_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(npm_runtime, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stderr="lockfile mismatch"),
    )

    with pytest.raises(BackendError, match="lockfile mismatch"):
        npm_runtime.run_npm_ci(tmp_path)


def test_run_npm_ci_succeeds_on_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(npm_runtime, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=0))

    npm_runtime.run_npm_ci(tmp_path)


def test_run_npm_build_raises_when_output_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(npm_runtime, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=0))

    with pytest.raises(BackendError):
        npm_runtime.run_npm_build(tmp_path)


def test_run_npm_build_returns_the_output_path_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(npm_runtime, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=0))
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    built = dist_dir / "tree-tui.mjs"
    built.write_text("", encoding="utf-8")

    result = npm_runtime.run_npm_build(tmp_path)

    assert result == built

"""Detect npm on PATH, and drive it to build the bundled TUI's Node.js
script from a package-embedded copy of `tui/`'s source. Kept as a
separate module from `node_runtime.py` -- npm and node are different
external devices with different detection/execution concerns, mirroring
the existing iTerm2/tmux backend split."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from ..errors import BackendError

_PACKAGE = "sukuna"
_SOURCE_SUBDIR = "_tui_source"
_BUILT_BUNDLE_SUBPATH = ("dist", "tree-tui.mjs")


def npm_executable() -> str | None:
    return shutil.which("npm")


def npm_available() -> bool:
    return npm_executable() is not None


def npm_status_message() -> str:
    npm_path = npm_executable()
    if npm_path is None:
        return "npm not found on PATH"
    return f"npm found on PATH: {npm_path}"


def _tui_source_traversable() -> Traversable:
    return resources.files(_PACKAGE).joinpath(_SOURCE_SUBDIR)


def tui_source_available() -> bool:
    return _tui_source_traversable().is_dir()


def _walk_source_files(root: Traversable) -> list[tuple[str, Traversable]]:
    """Depth-first walk of `root` via `Traversable.iterdir()`/`.is_dir()`
    only -- `importlib.resources.as_file()` is not used here because its
    directory-tree support is not guaranteed on Python 3.11 (this
    project's minimum). Returns (relative POSIX path, file Traversable)
    pairs in encounter order; callers that need a deterministic order
    must sort the result themselves."""
    collected: list[tuple[str, Traversable]] = []
    pending: list[tuple[str, Traversable]] = [("", root)]
    while pending:
        prefix, node = pending.pop()
        for entry in node.iterdir():
            relative_path = f"{prefix}{entry.name}"
            if entry.is_dir():
                pending.append((f"{relative_path}/", entry))
            else:
                collected.append((relative_path, entry))
    return collected


def embedded_tui_source_fingerprint() -> str | None:
    """A content fingerprint of the embedded `_tui_source` tree: identical
    trees always yield the same string, and any change to a file's bytes,
    relative path, or the set of files changes it. `None` when
    `_tui_source` is not embedded in this install (e.g. an older wheel
    built before this card)."""
    if not tui_source_available():
        return None
    files = sorted(
        _walk_source_files(_tui_source_traversable()), key=lambda item: item[0]
    )
    digest = hashlib.sha256()
    for relative_path, entry in files:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def copy_tui_source(dest: Path) -> None:
    """Recursively copies the embedded `_tui_source` tree to `dest`.
    `dest` must not already exist -- callers always pass a fresh subpath
    of a `tempfile.TemporaryDirectory()`, so this is a contract, not a
    merge/overwrite operation."""
    if not tui_source_available():
        raise BackendError("embedded tui source is not available in this install")
    if dest.exists():
        raise BackendError(f"copy_tui_source destination already exists: {dest}")
    dest.mkdir(parents=True)
    for relative_path, entry in _walk_source_files(_tui_source_traversable()):
        target = dest / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.read_bytes())


def _run_npm(args: list[str], source_dir: Path, *, timeout: float) -> None:
    npm_path = npm_executable()
    if npm_path is None:
        raise BackendError("npm executable not found on PATH")
    try:
        completed = subprocess.run(
            [npm_path, *args],
            cwd=source_dir,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except OSError as error:
        raise BackendError(f"npm {' '.join(args)} failed to start: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise BackendError(f"npm {' '.join(args)} timed out") from error
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "npm exited non-zero"
        )
        raise BackendError(f"npm {' '.join(args)} failed: {detail}")


def run_npm_ci(source_dir: Path, *, timeout: float = 300) -> None:
    _run_npm(["ci"], source_dir, timeout=timeout)


def run_npm_build(source_dir: Path, *, timeout: float = 120) -> Path:
    _run_npm(["run", "build"], source_dir, timeout=timeout)
    built = source_dir.joinpath(*_BUILT_BUNDLE_SUBPATH)
    if not built.is_file():
        raise BackendError(
            f"npm run build exited successfully but did not produce {built}"
        )
    return built

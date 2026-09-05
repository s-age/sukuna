"""Launch the bundled Node.js tree TUI as a subprocess. JSON is handed to
the Node process via a temp file + argv, never stdin."""

from __future__ import annotations

import importlib.resources
import json
import subprocess
import tempfile
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from ...errors import BackendError, ValidationError
from ..atomic_write import atomic_write
from ..node_runtime import (
    TUI_MIN_NODE_MAJOR,
    node_executable,
    node_satisfies_tui_minimum,
    node_status_message,
)
from ..registry import sukuna_state_dir

_BUNDLE_PACKAGE = "sukuna"
_BUNDLE_SUBPATH = ("_tui_bundle", "tree-tui.mjs")
_STATE_DIR_BUNDLE_FILENAME = "tree-tui.mjs"
_STATE_DIR_STAMP_FILENAME = "stamp.json"
_STAMP_FINGERPRINT_KEY = "tui_source_fingerprint"


def _bundle_traversable() -> Traversable:
    return importlib.resources.files(_BUNDLE_PACKAGE).joinpath(*_BUNDLE_SUBPATH)


def bundle_present() -> bool:
    """Whether the bundled `tree-tui.mjs` ships as a package resource with
    this install (the wheel's own `_tui_bundle/`) -- this does not cover a
    bundle built into the user's state dir by `sukuna-cli init`; see
    `state_dir_bundle_exists()` for that."""
    return _bundle_traversable().is_file()


def _state_dir_bundle_dir() -> Path:
    return sukuna_state_dir() / "tui_bundle"


def _state_dir_bundle_path() -> Path:
    return _state_dir_bundle_dir() / _STATE_DIR_BUNDLE_FILENAME


def _state_dir_stamp_path() -> Path:
    return _state_dir_bundle_dir() / _STATE_DIR_STAMP_FILENAME


def state_dir_bundle_exists() -> bool:
    return _state_dir_bundle_path().is_file()


def state_dir_stamp_fingerprint() -> str | None:
    """Never raises: a missing stamp file, unreadable file, malformed
    JSON, or a missing/non-string fingerprint key are all treated as "no
    stamp recorded" -- the same non-raising, bool/Optional discipline as
    `bundle_present()`/`state_dir_bundle_exists()`."""
    try:
        raw = json.loads(_state_dir_stamp_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    fingerprint = raw.get(_STAMP_FINGERPRINT_KEY)
    return fingerprint if isinstance(fingerprint, str) else None


def install_bundle_from_build(built_bundle_path: Path, *, fingerprint: str) -> None:
    """Installs a freshly built bundle into the state dir. `fingerprint`
    is computed by the caller (`usecase/init.py::attempt_tui_build()`, via
    `npm_runtime.embedded_tui_source_fingerprint()`) -- this function only
    writes what it is given. The bundle and its stamp are two separate
    atomic writes; a failure between them leaves `state_dir_bundle_usable()`
    (which requires both to agree) safely on the "not usable" side."""
    bundle_dir = _state_dir_bundle_dir()
    bundle_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(
        _state_dir_bundle_path(),
        built_bundle_path.read_bytes(),
        prefix=".tree-tui-",
    )
    stamp_content = json.dumps({_STAMP_FINGERPRINT_KEY: fingerprint}) + "\n"
    atomic_write(_state_dir_stamp_path(), stamp_content, prefix=".tui-stamp-")


def _write_payload_file(payload: dict[str, Any]) -> Path:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(payload, handle)
        return Path(handle.name)


def _run_node(node_path: str, bundle_path: Path, payload_path: Path) -> int:
    try:
        completed = subprocess.run(
            [node_path, str(bundle_path), str(payload_path)], check=False
        )
    except OSError as error:
        raise BackendError(f"failed to launch the tree TUI: {error}") from error
    return completed.returncode


def launch_tree_tui(
    payload: dict[str, Any], *, state_dir_bundle_usable: bool = False
) -> int:
    """Re-checks Node.js at invocation time regardless of what `sukuna-cli
    init` recorded -- the environment can change between `init` and actual
    use. stdin/stdout/stderr are inherited (not redirected) so Ink can
    occupy the terminal exclusively.

    Bundle resolution order: (1) the package-embedded bundle
    (`bundle_present()`) always wins when present -- unchanged behavior.
    (2) otherwise, when the caller has determined the state-dir bundle is
    usable (`state_dir_bundle_usable`, from `usecase/tui_bundle.py`'s
    version-stamp check) *and* it still exists on disk (a TOCTOU
    recheck, same discipline as `node_executable()` below), that bundle is
    used. (3) otherwise this raises `BackendError`, same as before but
    with a `sukuna-cli init` re-run pointer added to the message."""
    if not node_satisfies_tui_minimum():
        raise ValidationError(
            f"sukuna-cli tree --tui requires Node.js >={TUI_MIN_NODE_MAJOR} "
            f"on PATH (found: {node_status_message()})"
        )
    use_package_bundle = bundle_present()
    use_state_dir_bundle = (
        not use_package_bundle
        and state_dir_bundle_usable
        and _state_dir_bundle_path().is_file()
    )
    if not use_package_bundle and not use_state_dir_bundle:
        raise BackendError(
            "the tree TUI's bundled Node.js script was not found -- this "
            "sukuna install may be missing its Node bundle -- run "
            "`sukuna-cli init` again to rebuild it"
        )
    node_path = node_executable()
    if node_path is None:
        raise BackendError("node executable disappeared between checks")

    payload_path = _write_payload_file(payload)
    try:
        if use_package_bundle:
            with importlib.resources.as_file(_bundle_traversable()) as bundle_path:
                return _run_node(node_path, bundle_path, payload_path)
        return _run_node(node_path, _state_dir_bundle_path(), payload_path)
    finally:
        payload_path.unlink(missing_ok=True)

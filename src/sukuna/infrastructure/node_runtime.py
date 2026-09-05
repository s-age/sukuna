"""Detect whether Node.js is installed and new enough for `sukuna-cli tree
--tui`."""

from __future__ import annotations

import re
import shutil
import subprocess

TUI_MIN_NODE_MAJOR = 24

_VERSION_PATTERN = re.compile(r"^v(\d+)\.")


def node_executable() -> str | None:
    return shutil.which("node")


def _raw_version(node_path: str) -> str | None:
    try:
        completed = subprocess.run(
            [node_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = completed.stdout.strip()
    return version or None


def node_major_version(node_path: str) -> int | None:
    version = _raw_version(node_path)
    if version is None:
        return None
    match = _VERSION_PATTERN.match(version)
    if match is None:
        return None
    return int(match.group(1))


def node_satisfies_tui_minimum() -> bool:
    node_path = node_executable()
    if node_path is None:
        return False
    major = node_major_version(node_path)
    if major is None:
        return False
    return major >= TUI_MIN_NODE_MAJOR


def node_status_message() -> str:
    node_path = node_executable()
    if node_path is None:
        return "Node.js not found on PATH"
    version = _raw_version(node_path)
    match = _VERSION_PATTERN.match(version) if version is not None else None
    if version is None or match is None:
        return "Node.js version could not be determined"
    if int(match.group(1)) >= TUI_MIN_NODE_MAJOR:
        return f"Node.js {version} detected (>= {TUI_MIN_NODE_MAJOR}, OK)"
    return (
        f"Node.js {version} detected (< {TUI_MIN_NODE_MAJOR}, does not "
        "satisfy sukuna's TUI requirement)"
    )

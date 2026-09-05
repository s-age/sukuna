"""Read-only filesystem access to Claude Code's own session transcripts
(`~/.claude/projects/<encodedDir>/<sessionId>.jsonl`). This is the only
place that touches that filesystem tree; the encoding/matching rules it
applies live in `domain/service/session_log.py`."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..domain.service.session_log import (
    encode_project_dir_name,
    find_custom_title_match,
)

DEFAULT_CUSTOM_TITLE_SCAN_LINES = 20


def default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _read_leading_entries(path: Path, count: int) -> list[Any]:
    """Read and JSON-decode up to `count` leading lines of `path`,
    silently skipping blank or malformed ones -- defensive scanning of an
    undocumented format, not validation of a contract."""
    entries: list[Any] = []
    with path.open(encoding="utf-8") as handle:
        for _ in range(count):
            line = handle.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return entries


def _newest_match(matches: list[tuple[Path, float]]) -> str | None:
    """Shared mtime-newest-first tie-break for both `resolve_*` functions
    below: `None` if `matches` is empty, otherwise the path of the
    `(path, mtime)` pair with the largest mtime."""
    if not matches:
        return None
    newest_path, _ = max(matches, key=lambda match: match[1])
    return str(newest_path)


def resolve_session_log_path(
    worktree: str,
    name: str,
    not_before: str | None = None,
    *,
    scan_lines: int = DEFAULT_CUSTOM_TITLE_SCAN_LINES,
) -> str | None:
    """`None` when the worker's project directory doesn't exist yet, or no
    jsonl inside it has a leading `custom-title` entry matching `name`.
    This is a snapshot at call time, never re-resolved for a worker once it
    comes up empty.

    A name can match more than one jsonl: `respawn` reuses the same
    `WorkerRecord.name`, so `claude -n <name>` runs again and leaves a
    second, unrelated session file with the same `custom-title` behind.
    Filenames are session UUIDs -- unordered with respect to time -- so
    among matches the one with the newest mtime wins, on the assumption
    that the live session is the one still being appended to.

    `not_before` (an aware ISO-8601 string, typically
    `WorkerRecord.session_log_reset_at`) additionally filters out any
    matching candidate whose mtime predates it. Comparison is done in
    epoch space (the ISO string parsed once via
    `datetime.fromisoformat().timestamp()`)."""
    project_dir = default_projects_dir() / encode_project_dir_name(worktree)
    if not project_dir.is_dir():
        return None
    not_before_epoch = (
        datetime.fromisoformat(not_before).timestamp()
        if not_before is not None
        else None
    )
    matches: list[tuple[Path, float]] = []
    for jsonl_path in project_dir.glob("*.jsonl"):
        # A read/stat failure on one candidate (permission error, the file
        # vanishing mid-scan) must not sink the whole worker to `None` if
        # another candidate in the same directory is still readable and
        # matches -- only skip *this* file, not the worker's resolution.
        # If every candidate fails, `matches` stays empty and this still
        # returns `None`.
        try:
            entries = _read_leading_entries(jsonl_path, scan_lines)
            if not find_custom_title_match(entries, name):
                continue
            mtime = jsonl_path.stat().st_mtime
        except OSError:
            continue
        if not_before_epoch is not None and mtime < not_before_epoch:
            continue
        matches.append((jsonl_path, mtime))
    return _newest_match(matches)


def resolve_parent_session_log_path(parent_session_id: str) -> str | None:
    """`None` when no `<parent_session_id>.jsonl` exists under any
    worktree's project directory. Unlike `resolve_session_log_path()`,
    this needs no custom-title correlation:
    a parent session's id *is* its own Claude Code session UUID, and
    Claude Code names each session's jsonl after that UUID, so a direct
    glob across every worktree's project directory resolves it -- no
    `worktree` is recorded for parent sessions (they are not workers) to
    narrow the search to a single directory the way
    `resolve_session_log_path()` does. Session UUIDs are globally unique,
    so more than one match should not occur in practice; on the off
    chance it does, this falls back to the same
    mtime-newest-first tie-break as `resolve_session_log_path()` for
    consistency."""
    projects_dir = default_projects_dir()
    if not projects_dir.is_dir():
        return None
    filename = f"{parent_session_id}.jsonl"
    matches: list[tuple[Path, float]] = []
    # Both path components of this pattern are wildcards, so matching is
    # a pure `scandir()` enumeration -- no per-candidate `stat()`/`exists()`
    # call happens as a side effect of selection itself (unlike a pattern
    # ending in a literal filename, which pathlib resolves via `exists()`
    # before this loop ever sees the candidate, defeating the per-candidate
    # try/except below). The literal filename match happens here, in plain
    # Python, after enumeration.
    for jsonl_path in projects_dir.glob("*/*.jsonl"):
        if jsonl_path.name != filename:
            continue
        # Same per-candidate degrade as resolve_session_log_path(): a
        # stat() failure on one candidate must not sink the whole
        # resolution if another candidate is still readable.
        try:
            mtime = jsonl_path.stat().st_mtime
        except OSError:
            continue
        matches.append((jsonl_path, mtime))
    return _newest_match(matches)

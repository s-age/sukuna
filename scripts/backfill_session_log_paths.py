"""One-off migration: backfill `WorkerRecord.session_log_path` for every
currently-unresolved worker in registry.json.

`session_log_path` is resolved lazily, per worker, at state-transition time
(`infrastructure/registry.py`'s `mutate()`/`replace()` hook), so any worker
that predates that resolution stays permanently unresolved unless it
happens to go through another state transition. This script backfills
those once, using a single full-projects-directory scan (~0.5s for ~540
jsonl files) instead of a separate per-worktree rescan for every affected
worker.

Not registered as a `pyproject.toml` console script and not run
automatically by anything -- run directly, after `PYTHONPATH=src` is set:

    PYTHONPATH=src python scripts/backfill_session_log_paths.py [--registry PATH]

Safe to re-run: workers that already have a `session_log_path` are
skipped, and the actual write decision goes through the exact same
`attach_session_log_path_if_unresolved()` domain function
`infrastructure/registry.py`'s hook uses in production (reused here by
constructing `Registry` with this script's index-backed resolver injected
in its place, rather than a separate hand-rolled write) -- so a
`STARTING` worker (mid-respawn) is skipped here exactly as it would be
during a normal state transition, and left for that transition to resolve
instead.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from sukuna.domain.service.session_log import (
    encode_project_dir_name,
    find_custom_title_match,
)
from sukuna.infrastructure.claude_projects import (
    DEFAULT_CUSTOM_TITLE_SCAN_LINES,
    default_projects_dir,
)
from sukuna.infrastructure.registry import Registry

# (encoded project dir name, worker name) -> (jsonl path, its mtime).
Index = dict[tuple[str, str], tuple[str, float]]


def _read_leading_entries(path: Path, count: int) -> list[object]:
    """Self-contained copy of `claude_projects.py`'s private
    `_read_leading_entries()` -- this script deliberately doesn't import
    that underscore-prefixed helper (nor call the per-worker
    `resolve_session_log_path()` at all): the whole point of the index in
    `build_index()` below is a single pass over the filesystem, not one
    scan per worker."""
    entries: list[object] = []
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


def build_index(projects_dir: Path, names: frozenset[str]) -> Index:
    """One pass over every `<projects_dir>/<dir>/*.jsonl`, reusing
    `find_custom_title_match()` (the same domain rule
    `resolve_session_log_path()` itself uses) against `names` -- the set of
    worker names actually worth indexing, not an open-ended name space.
    A jsonl's leading entries are decoded exactly once regardless of how
    many names are tested against them.

    Winner-selection rule (card §4.5): when more than one jsonl in the same
    directory matches the same worker name (a respawn leaves the old
    session file behind alongside the new one), the entry with the largest
    mtime wins -- picked explicitly here, not by scan-order overwrite,
    since scan order says nothing about recency."""
    index: Index = {}
    if not projects_dir.is_dir() or not names:
        return index
    for dir_entry in sorted(projects_dir.iterdir()):
        if not dir_entry.is_dir():
            continue
        for jsonl_path in sorted(dir_entry.glob("*.jsonl")):
            try:
                entries = _read_leading_entries(
                    jsonl_path, DEFAULT_CUSTOM_TITLE_SCAN_LINES
                )
                mtime = jsonl_path.stat().st_mtime
            except OSError:
                continue
            for name in names:
                if not find_custom_title_match(entries, name):
                    continue
                # No `break` here: a session renamed mid-run can leave more
                # than one `custom-title` entry among the leading lines, so
                # one jsonl can legitimately match more than one requested
                # name -- matching `resolve_session_log_path()`'s own
                # per-name `find_custom_title_match()` semantics exactly,
                # rather than assuming a file matches at most one name.
                key = (dir_entry.name, name)
                existing = index.get(key)
                if existing is None or mtime > existing[1]:
                    index[key] = (str(jsonl_path), mtime)
    return index


def make_index_resolver(index: Index):
    """Thin wrapper matching `Registry`'s injectable
    `SessionLogResolver` shape (`(worktree, name, not_before) -> str | None`)
    over the pre-built `index` -- an index lookup instead of a filesystem
    probe. `not_before` is compared in epoch space, the same discipline
    `claude_projects.resolve_session_log_path()` uses, rather than a raw
    ISO-string/float comparison."""

    def _resolve(worktree: str, name: str, not_before: str | None) -> str | None:
        entry = index.get((encode_project_dir_name(worktree), name))
        if entry is None:
            return None
        path, mtime = entry
        if (
            not_before is not None
            and mtime < datetime.fromisoformat(not_before).timestamp()
        ):
            return None
        return path

    return _resolve


def backfill(registry: Registry, projects_dir: Path) -> int:
    """Rebind `registry` to an index-backed resolver (so its normal
    `mutate()` hook -- `attach_session_log_path_if_unresolved()`, the same
    domain function production uses -- does the actual resolve-and-write
    decision, rather than this script reimplementing it) and re-run every
    still-unresolved worker's transition-time hook once. Returns the count
    of workers newly resolved."""
    names = frozenset(
        worker.name for worker in registry.list() if worker.session_log_path is None
    )
    index = build_index(projects_dir, names)
    indexed_registry = Registry(
        registry.path, session_log_resolver=make_index_resolver(index)
    )
    updated = 0
    for worker in indexed_registry.list():
        if worker.session_log_path is not None:
            continue
        result = indexed_registry.mutate(worker.name, lambda _current: None)
        if result.session_log_path is not None:
            updated += 1
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="Path to registry.json (defaults to sukuna's own state dir).",
    )
    args = parser.parse_args()
    registry = Registry(args.registry) if args.registry is not None else Registry()
    updated = backfill(registry, default_projects_dir())
    print(f"backfilled session_log_path for {updated} worker(s)")


if __name__ == "__main__":
    main()

"""Thin filesystem-existence check used by preflight validation.

`domain.service.spawn_preflight.preflight_one_spec()` needs to know whether
a `repo`/`worktree` string names an existing directory, but the domain layer
must not perform I/O itself (layer responsibility policy, `.claude/CLAUDE.md`)
-- the actual `expanduser()`/`resolve()`/`is_dir()` calls live here instead,
and the caller (the usecase layer) passes this function into the domain
function as a dependency.
"""

from __future__ import annotations

from pathlib import Path


def resolve_existing_directory(raw: str) -> Path | None:
    """Resolve `raw` to an absolute path and return it if it names an
    existing directory, `None` otherwise."""
    path = Path(raw).expanduser().resolve()
    return path if path.is_dir() else None

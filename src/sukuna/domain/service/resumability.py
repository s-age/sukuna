"""Pure business rule for whether a worker's session can be resumed from
the CLI's current working directory. No filesystem access here -- `cwd`
must already be normalized the same way `WorkerRecord.worktree` is at
spawn time (`expanduser().resolve()`, `spawn_preflight.py`); the caller
(CLI layer) is responsible for that normalization."""

from __future__ import annotations

from ..entity.worker_record import WorkerRecord


def is_resumable(worker: WorkerRecord, cwd: str) -> bool:
    """`worker` is resumable from `cwd` exactly when the two name the same
    directory -- `worker_command()`/`resume_command()` both `cd` into
    `worktree` before invoking `claude`. A plain string comparison is
    exact (including through a symlinked worktree path) as long as both
    sides were resolved with the same rule."""
    return worker.worktree == cwd

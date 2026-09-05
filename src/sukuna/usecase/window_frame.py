"""Shared window-frame save/restore, used by `close`/`spawn`/`resize`: each
performs a pane/window operation that can drift an iTerm2 window's frame
(destroying a pane, repeated resize() convergence attempts), and wants the
frame restored to its pre-operation value afterward regardless of how the
operation exits."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from ..errors import BackendError
from ..infrastructure.terminal import operations as terminal_ops


@contextmanager
def preserve_window_frame(*, backend: str, window_ref: str | None) -> Iterator[None]:
    """Best-effort: a failed read leaves the frame unrestored, and a failed
    write is swallowed rather than raised -- neither may turn a caller's
    own success/failure outcome into something it wasn't. `get_window_frame`/
    `set_window_frame` are no-ops off iTerm2, so this is a harmless bracket
    for tmux callers. The write happens in a `finally`, so it always runs."""
    frame = None
    try:
        frame = terminal_ops.get_window_frame(backend=backend, window_ref=window_ref)
    except BackendError:
        frame = None
    try:
        yield
    finally:
        if frame is not None:
            try:
                terminal_ops.set_window_frame(
                    backend=backend, window_ref=window_ref, frame=frame
                )
            except BackendError:
                pass

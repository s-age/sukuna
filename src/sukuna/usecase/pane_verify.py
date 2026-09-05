"""Shared point-in-time pane verify helper: folds `infer_backend()` +
`terminal_ops.verify()` + `CrossBufferError` into a single tri-state
verdict."""

from __future__ import annotations

from ..errors import CrossBufferError
from ..infrastructure.terminal import operations as terminal_ops
from ..infrastructure.terminal.resolver import infer_backend


def verify_pane_exists(pane_ref: str) -> bool | None:
    """`True` = the pane positively verifies as alive, `False` = it
    positively verifies as gone, `None` = the verify call itself failed
    (`CrossBufferError` -- the backend tool is unavailable, not "the pane
    is gone" -- or a non-`ok` response). Which side of `None` is safe is
    the caller's policy, not this function's."""
    backend = infer_backend(pane_ref)
    try:
        verified = terminal_ops.verify(backend=backend, pane_ref=pane_ref)
    except CrossBufferError:
        return None
    return verified.exists if verified.ok else None

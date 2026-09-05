"""Capture the visible screen contents of a managed worker's pane."""

from __future__ import annotations

from typing import Any

from ..domain.mapper.command_mapper import validate_name
from ..errors import ValidationError
from ..infrastructure.registry import Registry
from ..infrastructure.terminal import operations as terminal_ops
from ..infrastructure.terminal.resolver import infer_backend


def capture(registry: Registry, name: str) -> dict[str, Any]:
    name = validate_name(name)
    worker = registry.get(name)
    if worker.pane_ref is None:
        raise ValidationError(f"worker {name!r} has no pane to capture")
    result = terminal_ops.capture(
        backend=infer_backend(worker.pane_ref), pane_ref=worker.pane_ref
    )
    return result.model_dump()

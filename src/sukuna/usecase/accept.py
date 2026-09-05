"""Accept a reported worker's result."""

from __future__ import annotations

from typing import Any

from ..domain.entity.worker_record import WorkerState
from ..domain.mapper.registry_mapper import record_to_dict
from ..infrastructure.registry import Registry


def accept(registry: Registry, worker: str) -> dict[str, Any]:
    return record_to_dict(registry.transition(worker, WorkerState.ACCEPTED))

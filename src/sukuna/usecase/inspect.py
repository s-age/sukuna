"""Show a worker record, or the whole registry."""

from __future__ import annotations

from typing import Any

from ..domain.mapper.registry_mapper import record_to_dict
from ..infrastructure.registry import Registry


def inspect(registry: Registry, worker: str | None) -> dict[str, Any]:
    if worker:
        return record_to_dict(registry.get(worker))
    return {"workers": [record_to_dict(item) for item in registry.list()]}

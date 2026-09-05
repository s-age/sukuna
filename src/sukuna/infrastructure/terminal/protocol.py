"""Structural type every terminal backend satisfies."""

from __future__ import annotations

from typing import Any, Protocol


class TerminalBackend(Protocol):
    def run(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def diagnostics(self) -> dict[str, Any]: ...

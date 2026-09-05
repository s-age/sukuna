"""Pure business rule for opt-in `model` membership checking (no I/O)."""

from __future__ import annotations

from ...errors import ValidationError


def validate_model_membership(model: str, known_model_ids: list[str]) -> None:
    if model not in known_model_ids:
        raise ValidationError(f"model {model!r} is not in the known model catalog")

import pytest

from sukuna.domain.service.model_validation import validate_model_membership
from sukuna.errors import ValidationError


def test_validate_model_membership_accepts_a_known_id() -> None:
    validate_model_membership("claude-sonnet-5", ["claude-sonnet-5", "claude-opus-5"])


def test_validate_model_membership_rejects_an_unknown_id() -> None:
    with pytest.raises(ValidationError):
        validate_model_membership(
            "clade-sonnet-5", ["claude-sonnet-5", "claude-opus-5"]
        )

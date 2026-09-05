"""`should_attempt_tui_build`'s 4-input decision table, same shape as
test_domain_service_tree_mode.py. Note `bundle_already_present`'s
gate-satisfied value is `False` (not `True`, unlike the other three
inputs) -- a bundle already being usable is itself a reason not to
attempt a build (idempotent `init` re-runs must not rebuild every time),
per `tui_build.py`'s module docstring."""

import pytest

from sukuna.domain.service.tui_build import should_attempt_tui_build


def test_attempts_build_when_every_gate_is_satisfied() -> None:
    assert (
        should_attempt_tui_build(
            tui_enabled_answer=True,
            npm_available=True,
            tui_source_available=True,
            bundle_already_present=False,
        )
        is True
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "tui_enabled_answer": False,
            "npm_available": True,
            "tui_source_available": True,
            "bundle_already_present": False,
        },
        {
            "tui_enabled_answer": True,
            "npm_available": False,
            "tui_source_available": True,
            "bundle_already_present": False,
        },
        {
            "tui_enabled_answer": True,
            "npm_available": True,
            "tui_source_available": False,
            "bundle_already_present": False,
        },
        {
            "tui_enabled_answer": True,
            "npm_available": True,
            "tui_source_available": True,
            "bundle_already_present": True,
        },
    ],
)
def test_skips_build_when_any_single_gate_fails(kwargs: dict[str, bool]) -> None:
    assert should_attempt_tui_build(**kwargs) is False


def test_skips_build_when_every_gate_fails() -> None:
    assert (
        should_attempt_tui_build(
            tui_enabled_answer=False,
            npm_available=False,
            tui_source_available=False,
            bundle_already_present=True,
        )
        is False
    )

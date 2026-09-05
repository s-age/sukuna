"""`should_launch_tui`'s 4-input decision table -- `sukuna-cli tree` with
neither `--tui` nor `--text` auto-launches the TUI only when every gate is
satisfied (`tui_enabled` is the opt-in switch; every other unmet gate
falls back to text, never an error)."""

import pytest

from sukuna.domain.service.tree_mode import should_launch_tui


def test_launches_when_every_gate_is_satisfied() -> None:
    assert (
        should_launch_tui(
            tui_enabled=True,
            stdin_is_tty=True,
            stdout_is_tty=True,
            node_and_bundle_ok=True,
        )
        is True
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "tui_enabled": False,
            "stdin_is_tty": True,
            "stdout_is_tty": True,
            "node_and_bundle_ok": True,
        },
        {
            "tui_enabled": True,
            "stdin_is_tty": False,
            "stdout_is_tty": True,
            "node_and_bundle_ok": True,
        },
        {
            "tui_enabled": True,
            "stdin_is_tty": True,
            "stdout_is_tty": False,
            "node_and_bundle_ok": True,
        },
        {
            "tui_enabled": True,
            "stdin_is_tty": True,
            "stdout_is_tty": True,
            "node_and_bundle_ok": False,
        },
    ],
)
def test_falls_back_to_text_when_any_single_gate_fails(kwargs: dict[str, bool]) -> None:
    assert should_launch_tui(**kwargs) is False


def test_falls_back_to_text_when_every_gate_fails() -> None:
    assert (
        should_launch_tui(
            tui_enabled=False,
            stdin_is_tty=False,
            stdout_is_tty=False,
            node_and_bundle_ok=False,
        )
        is False
    )

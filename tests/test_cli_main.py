"""`cli_shared.run()` is the error-handling body shared by both entry
points' `main()` (`sukuna.cli.main`) and `main_cli()`
(`sukuna.cli_human.main_cli`). These tests exercise it directly with a
throwaway parser/handler, independent of either CLI's actual
subcommands."""

import argparse
import json
from collections.abc import Callable

import pytest

from sukuna.cli_shared import run
from sukuna.errors import ValidationError


def _parser_with_handler(
    handler: Callable[[argparse.Namespace], int],
) -> Callable[[], argparse.ArgumentParser]:
    def parser() -> argparse.ArgumentParser:
        result = argparse.ArgumentParser(prog="test")
        result.set_defaults(handler=handler)
        return result

    return parser


def test_run_returns_130_and_prints_nothing_to_stdout_on_keyboard_interrupt(
    capsys,
) -> None:
    def _raise_keyboard_interrupt(_: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    exit_code = run(_parser_with_handler(_raise_keyboard_interrupt), [])

    assert exit_code == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() != ""


def test_run_reports_an_uncaught_os_error_as_an_os_error_envelope(capsys) -> None:
    def _raise_os_error(_: argparse.Namespace) -> int:
        raise OSError(28, "No space left on device")

    exit_code = run(_parser_with_handler(_raise_os_error), [])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "OS_ERROR"
    assert "No space left on device" in payload["message"]


def test_run_reports_a_cross_buffer_error_as_an_envelope_with_exit_code_2(
    capsys,
) -> None:
    def _raise_validation_error(_: argparse.Namespace) -> int:
        raise ValidationError("boom")

    exit_code = run(_parser_with_handler(_raise_validation_error), [])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "VALIDATION_ERROR"
    assert payload["message"] == "boom"


def test_run_lets_a_normal_handler_run_through_unaffected(capsys) -> None:
    def _handler(_: argparse.Namespace) -> int:
        print("ok")
        return 0

    exit_code = run(_parser_with_handler(_handler), [])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "ok"


def test_run_with_empty_argv_runs_the_handler_when_help_when_no_args_is_unset(
    capsys,
) -> None:
    """Default (`help_when_no_args` unset) preserves prior behavior: an
    explicit empty argv list is just an argv list, not a signal to print
    help. `sukuna.cli.main` / `sukuna.cli_human.main_cli` opt in via the
    flag; this generic entry point (used by callers with no subcommands)
    does not."""

    def _handler(_: argparse.Namespace) -> int:
        print("ok")
        return 0

    exit_code = run(_parser_with_handler(_handler), [])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "ok"


def test_run_with_help_when_no_args_prints_help_and_returns_0_on_empty_argv(
    capsys,
) -> None:
    def _handler(_: argparse.Namespace) -> int:
        raise AssertionError("handler must not run when argv is empty")

    exit_code = run(_parser_with_handler(_handler), [], help_when_no_args=True)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert captured.err == ""


def test_run_with_help_when_no_args_still_rejects_unrecognized_nonempty_argv(
    capsys,
) -> None:
    """`help_when_no_args` only special-cases the *empty* argv; a nonempty
    but invalid argv still goes through normal argparse error handling
    (`parser.error()` -> `SystemExit(2)`) rather than being swallowed."""

    def _handler(_: argparse.Namespace) -> int:
        raise AssertionError("handler must not run on a parse error")

    with pytest.raises(SystemExit) as excinfo:
        run(_parser_with_handler(_handler), ["--unused"], help_when_no_args=True)

    assert excinfo.value.code == 2
    assert capsys.readouterr().out == ""

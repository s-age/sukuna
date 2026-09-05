"""Both entry points opt into `cli_shared.run()`'s `help_when_no_args`
(see `test_cli_main.py` for the flag's generic behavior): invoking either
CLI with zero arguments must print help and exit 0, not argparse's default
`error: the following arguments are required: operation` / exit 2."""

import sys

from sukuna.cli import main
from sukuna.cli_human import main_cli


def test_sukuna_with_no_args_prints_help_and_returns_0(capsys) -> None:
    exit_code = main([])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "usage: sukuna" in captured.out
    assert captured.err == ""


def test_sukuna_cli_with_no_args_prints_help_and_returns_0(capsys) -> None:
    exit_code = main_cli([])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "usage: sukuna-cli" in captured.out
    assert captured.err == ""


def test_sukuna_with_argv_none_reads_the_real_process_argv(monkeypatch, capsys) -> None:
    """`argv=None` (the default when invoked as `sukuna` with no arguments
    at all) falls back to `sys.argv[1:]`, matching argparse's own default
    -- not to an empty list, which would make every real no-args
    invocation look like an explicit `sukuna []` call instead."""
    monkeypatch.setattr(sys, "argv", ["sukuna"])

    exit_code = main()

    assert exit_code == 0
    assert "usage: sukuna" in capsys.readouterr().out


def test_sukuna_cli_with_argv_none_reads_the_real_process_argv(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(sys, "argv", ["sukuna-cli"])

    exit_code = main_cli()

    assert exit_code == 0
    assert "usage: sukuna-cli" in capsys.readouterr().out

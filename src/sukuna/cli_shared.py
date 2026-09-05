"""Plumbing shared between the agent-facing `sukuna` CLI (cli.py) and the
human-facing `sukuna-cli` CLI (cli_human.py): JSON output, registry
resolution from `--registry`, and `main()`'s top-level error handling."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .errors import CrossBufferError
from .infrastructure.registry import Registry


def emit(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def registry_from_args(args: argparse.Namespace) -> Registry:
    return Registry(Path(args.registry) if args.registry else None)


def run(
    parser_factory: Callable[[], argparse.ArgumentParser],
    argv: Sequence[str] | None,
    *,
    help_when_no_args: bool = False,
) -> int:
    built_parser = parser_factory()
    effective_argv = sys.argv[1:] if argv is None else argv
    if help_when_no_args and not effective_argv:
        built_parser.print_help()
        return 0
    args = built_parser.parse_args(argv)
    try:
        exit_code: int = args.handler(args)
    except CrossBufferError as error:
        emit({"ok": False, "error": error.code, "message": str(error)})
        return 2
    except OSError as error:
        emit({"ok": False, "error": "OS_ERROR", "message": str(error)})
        return 2
    except KeyboardInterrupt:
        # A user-driven interrupt (e.g. Ctrl-C during a `sukuna-cli init`
        # prompt) is not an error result -- the stdout JSON contract stays
        # "no output" rather than growing an EOF-style `ValidationError`.
        # Exit 130 follows the SIGINT convention (128 + signal number 2).
        print("aborted", file=sys.stderr)
        return 130
    return exit_code

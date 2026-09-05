"""Command-line interface for safe iTerm2/tmux worker management (the
agent-facing `sukuna` entry point). The human-facing `sukuna-cli` entry
point (`doctor`/`tree`/`init`) lives in `sukuna.cli_human`; shared plumbing
(`emit`, registry resolution, `main()`'s error handling) lives in
`sukuna.cli_shared`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cli_shared import emit
from .cli_shared import registry_from_args as _registry
from .cli_shared import run as _run
from .errors import ValidationError
from .usecase import registry_access
from .usecase.accept import accept as accept_use_case
from .usecase.capture import capture as capture_use_case
from .usecase.close import close as close_use_case
from .usecase.inspect import inspect as inspect_use_case
from .usecase.reconcile import reconcile as reconcile_use_case
from .usecase.resize import resize as focus_use_case
from .usecase.respawn import respawn as respawn_use_case
from .usecase.spawn import preflight_spawn_specs as preflight_spawn_specs_use_case
from .usecase.spawn import spawn_many as spawn_many_use_case
from .usecase.state import state as state_use_case

# Distinct from the `2` that `main()` returns for a propagated `CrossBufferError`
# (e.g. a pre-flight rejection of the whole batch): this code means the batch
# itself was processed, but one or more elements failed at runtime (best-effort).
#
# `Registry` converts filesystem `OSError`s (disk full, permission denied,
# etc.) into `RegistryIOError`, a `CrossBufferError` -- so a registry I/O
# failure on one element of a batch lands here (exit 3, that element in
# `failed`), not in the bare `except OSError` below. That bare handler is a
# last-resort catch-all for `OSError`s from code paths outside the registry
# (e.g. reading stdin); reaching it does NOT imply "pane operations ran
# zero times" the way it does for a pre-flight `CrossBufferError` -- by the
# time an uncaught `OSError` can occur, prior batch elements may already
# have spawned live panes.
SPAWN_PARTIAL_FAILURE_EXIT_CODE = 3


def _worker_registry(args: argparse.Namespace, name: str) -> Any:
    """A `Registry` scoped to `name`'s shard -- or, with `--registry` given
    (test/isolation override), the single file it names, bypassing
    sharding entirely. Used by every by-name subcommand (state/accept/
    close/respawn/capture/focus --worker)."""
    if args.registry:
        return _registry(args)
    registry_access.ensure_migrated()
    return registry_access.registry_for_worker(name)


def _spawn_registry(args: argparse.Namespace, parent_session_id: str | None) -> Any:
    """A `Registry` (override) or a per-spec shard resolver (sharded,
    `spawn_many()`'s `Registry | Callable[[SpawnSpec], Registry]`
    contract) for `sukuna spawn`."""
    if args.registry:
        return _registry(args)
    registry_access.ensure_migrated()
    return registry_access.spawn_registry_resolver(
        parent_session_id=parent_session_id, cwd=str(Path.cwd().resolve())
    )


def _all_registry(args: argparse.Namespace) -> Any:
    """A `Registry` (override) or a read-only merged view across every
    shard (sharded), for `inspect` with no `--worker`."""
    if args.registry:
        return _registry(args)
    registry_access.ensure_migrated()
    return registry_access.all_workers_view()


def _read_spawn_specs_from_stdin() -> list[Any]:
    if sys.stdin.isatty():
        raise ValidationError(
            "sukuna spawn reads a JSON array of specs from stdin; pipe it in "
            "(e.g. echo '[{...}]' | sukuna spawn)"
        )
    raw = sys.stdin.read()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValidationError(f"stdin must contain valid JSON: {error}") from error
    if not isinstance(parsed, list):
        raise ValidationError("stdin JSON must be a top-level array of spawn specs")
    return parsed


def handle_spawn(args: argparse.Namespace) -> int:
    parent_session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    specs = preflight_spawn_specs_use_case(
        _read_spawn_specs_from_stdin(), parent_session_id=parent_session_id
    )
    result = spawn_many_use_case(
        _spawn_registry(args, parent_session_id),
        specs,
        parent_session_id=parent_session_id,
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        # Only the real sharded path needs the full cross-shard name
        # universe (Layer 1) -- with `--registry`, that one
        # file already is the whole universe, so `spawn_many()`'s own
        # fallback (derive from the registries this batch touched) is
        # exactly correct and must not be overridden with the real
        # default state dir's shards.
        list_persisted_names=(
            None if args.registry else registry_access.persisted_worker_names
        ),
    )
    emit(result)
    return SPAWN_PARTIAL_FAILURE_EXIT_CODE if result["failed"] else 0


def handle_inspect(args: argparse.Namespace) -> int:
    registry = (
        _worker_registry(args, args.worker) if args.worker else _all_registry(args)
    )
    emit(inspect_use_case(registry, args.worker))
    return 0


def handle_state(args: argparse.Namespace) -> int:
    emit(state_use_case(_worker_registry(args, args.worker), args.worker, args.state))
    return 0


def handle_accept(args: argparse.Namespace) -> int:
    emit(accept_use_case(_worker_registry(args, args.worker), args.worker))
    return 0


def handle_close(args: argparse.Namespace) -> int:
    emit(close_use_case(_worker_registry(args, args.worker), args.worker))
    return 0


def handle_respawn(args: argparse.Namespace) -> int:
    parent_session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    emit(
        respawn_use_case(
            _worker_registry(args, args.worker),
            args.worker,
            parent_session_id=parent_session_id,
        )
    )
    return 0


def handle_capture(args: argparse.Namespace) -> int:
    emit(capture_use_case(_worker_registry(args, args.worker), args.worker))
    return 0


def handle_reconcile(args: argparse.Namespace) -> int:
    if args.registry:
        emit(reconcile_use_case(_registry(args), prune=args.prune))
        return 0
    registry_access.ensure_migrated()
    emit(registry_access.reconcile_all_shards(prune=args.prune))
    return 0


def handle_focus(args: argparse.Namespace) -> int:
    if args.worker:
        emit(focus_use_case(_worker_registry(args, args.worker), args.worker))
        return 0
    if args.registry:
        emit(focus_use_case(_registry(args), None))
        return 0
    registry_access.ensure_migrated()
    registry = registry_access.registry_for_root(
        parent_session_id=os.environ.get("CLAUDE_CODE_SESSION_ID"),
        cwd=str(Path.cwd().resolve()),
    )
    # Gate 2 (own pane matches a live sukuna worker record) needs the full
    # cross-shard universe: a worker's own `WorkerRecord` may live in a
    # different shard than `registry` above (see the design's "sharding
    # and read scope" -- `registry`'s single shard only reliably covers
    # this session's own spawn history, not where a worker's own record
    # landed).
    emit(
        focus_use_case(registry, None, pane_registry=registry_access.all_workers_view())
    )
    return 0


if TYPE_CHECKING:
    _SubParsers = argparse._SubParsersAction[argparse.ArgumentParser]


def _add_spawn_subcommand(subcommands: _SubParsers) -> None:
    spawn = subcommands.add_parser(
        "spawn",
        help="create and start one or more managed named workers from a JSON array on stdin",
        description=(
            "Reads a JSON array from stdin; each element is an object with required keys "
            '"role", "repo", "worktree" and optional keys "goal" (free-text, stored in the '
            'registry only, never passed to the `claude` command line), "parent_worker" '
            "(name of the worker that spawned this one, for nested `tree` display; not "
            'checked against the registry at spawn time), "active_pane_width" (percent '
            "width to resize the newly spawned pane to when it is split horizontally; "
            'absent or `null` falls back to the configured default) and "model" (passed '
            "through as `claude --model <value>`; absent or `null` starts the worker with "
            "`claude`'s own default model, and is also recorded in the registry. If "
            "$ANTHROPIC_API_KEY is set in this process's environment, a given `model` is "
            "opt-in checked against the Anthropic models-list API (cached locally; a cache "
            "hit needs no network call) before spawning, and the batch element is rejected "
            "if it isn't a known model id or the check itself fails; without "
            "$ANTHROPIC_API_KEY, `model` is passed through unchecked). Spawn a single "
            "worker by passing an array with exactly one element. An empty array is "
            "rejected."
        ),
    )
    spawn.set_defaults(handler=handle_spawn)


def _add_inspect_subcommand(subcommands: _SubParsers) -> None:
    inspect = subcommands.add_parser(
        "inspect", help="show a worker or registry contents"
    )
    inspect.add_argument("--worker", help="a worker's name")
    inspect.set_defaults(handler=handle_inspect)


def _add_state_subcommand(subcommands: _SubParsers) -> None:
    state = subcommands.add_parser(
        "state", help="apply a checked worker lifecycle transition"
    )
    state.add_argument("--worker", required=True, help="a worker's name")
    state.add_argument("--state", required=True, help="target lifecycle state")
    state.set_defaults(handler=handle_state)


def _add_accept_subcommand(subcommands: _SubParsers) -> None:
    accept = subcommands.add_parser("accept", help="accept a reported worker result")
    accept.add_argument("--worker", required=True, help="a worker's name")
    accept.set_defaults(handler=handle_accept)


def _add_close_subcommand(subcommands: _SubParsers) -> None:
    close = subcommands.add_parser("close", help="close an accepted managed worker")
    close.add_argument("--worker", required=True, help="a worker's name")
    close.set_defaults(handler=handle_close)


def _add_respawn_subcommand(subcommands: _SubParsers) -> None:
    respawn = subcommands.add_parser(
        "respawn", help="reopen a pane-less terminal worker via claude --resume"
    )
    respawn.add_argument("--worker", required=True, help="a worker's name")
    respawn.set_defaults(handler=handle_respawn)


def _add_capture_subcommand(subcommands: _SubParsers) -> None:
    capture = subcommands.add_parser(
        "capture", help="read a managed worker's visible pane contents"
    )
    capture.add_argument("--worker", required=True, help="a worker's name")
    capture.set_defaults(handler=handle_capture)


def _add_reconcile_subcommand(subcommands: _SubParsers) -> None:
    reconcile = subcommands.add_parser(
        "reconcile",
        help="verify all managed workers' panes still exist, failing the ones that don't",
    )
    reconcile.add_argument(
        "--prune",
        action="store_true",
        help=(
            "also retire CLOSED/FAILED(pane-less) records older than the configured "
            "retention_days (see `sukuna-cli init`); unlike the automatic thinning "
            "`sukuna spawn` already does, this bypasses the once-per-day throttle, so "
            "a just-shortened retention_days takes effect immediately"
        ),
    )
    reconcile.set_defaults(handler=handle_reconcile)


def _add_focus_subcommand(subcommands: _SubParsers) -> None:
    focus = subcommands.add_parser(
        "focus",
        help="resize a pane to the configured active_pane_width (default: the orchestrator's own pane)",
    )
    focus.add_argument(
        "--worker",
        help="a worker's name (default: the orchestrator's own pane, resolved from the environment)",
    )
    focus.set_defaults(handler=handle_focus)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="sukuna")
    result.add_argument(
        "--registry", help="override registry path (for testing or isolation)"
    )
    subcommands = result.add_subparsers(dest="operation", required=True)

    _add_spawn_subcommand(subcommands)
    _add_inspect_subcommand(subcommands)
    _add_state_subcommand(subcommands)
    _add_accept_subcommand(subcommands)
    _add_close_subcommand(subcommands)
    _add_respawn_subcommand(subcommands)
    _add_capture_subcommand(subcommands)
    _add_reconcile_subcommand(subcommands)
    _add_focus_subcommand(subcommands)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    return _run(parser, argv, help_when_no_args=True)


if __name__ == "__main__":
    raise SystemExit(main())

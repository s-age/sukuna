"""Command-line interface for the human-facing `sukuna-cli` commands:
`doctor`, `tree`, `init`. Split out of `sukuna.cli` (the agent-facing
`sukuna` entry point) because these three are TTY-oriented or
diagnostic/read-only, never invoked by the orchestrator Skill's
machine-driven procedure."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cli_shared import emit
from .cli_shared import registry_from_args as _registry
from .cli_shared import run as _run
from .errors import BackendError, ValidationError
from .presentation.consent_text import CONSENT_TEXT, TUI_CONSENT_TEXT
from .usecase import registry_access
from .usecase.doctor import doctor as doctor_use_case
from .usecase.init import (
    InitDecisions,
    attempt_tui_build,
    backend_prompt_precondition,
    check_active_pane_width,
    check_retention_days,
    npm_status_message,
    pending_hook_matchers,
    should_run_tui_build,
    tui_node_status,
)
from .usecase.init import init as init_use_case
from .usecase.tree import launch_tree_tui as launch_tree_tui_use_case
from .usecase.tree import should_auto_launch_tui as should_auto_launch_tui_use_case
from .usecase.tree import tree as tree_use_case


def _tree_registry(args: argparse.Namespace) -> Any:
    """A `Registry` (test/isolation `--registry` override, bypassing
    sharding entirely) or a read-only merged view across every shard
    (sharded, the default): `tree`'s output must reflect every worker
    regardless of which shard it landed in."""
    if args.registry:
        return _registry(args)
    registry_access.ensure_migrated()
    return registry_access.all_workers_view()


def handle_tree(args: argparse.Namespace) -> int:
    registry = _tree_registry(args)
    # `str(Path.cwd().resolve())` matches the normalization
    # `WorkerRecord.worktree` already went through at spawn time
    # (`domain/service/spawn_preflight.py`) -- see
    # `domain/service/resumability.py`.
    cwd = str(Path.cwd().resolve())
    if args.tui:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValidationError(
                "sukuna-cli tree --tui requires an interactive terminal "
                "(stdin and stdout must both be a TTY)"
            )
        return launch_tree_tui_use_case(
            registry,
            since=args.since,
            to=args.to,
            cwd=cwd,
            resumable_only=args.resumable_only,
        )
    auto_tui = not args.text and should_auto_launch_tui_use_case(
        stdin_is_tty=sys.stdin.isatty(), stdout_is_tty=sys.stdout.isatty()
    )
    if auto_tui:
        return launch_tree_tui_use_case(
            registry,
            since=args.since,
            to=args.to,
            cwd=cwd,
            resumable_only=args.resumable_only,
        )
    print(
        tree_use_case(
            registry,
            limit=args.limit,
            since=args.since,
            to=args.to,
            cwd=cwd,
            resumable_only=args.resumable_only,
        )
    )
    return 0


def handle_doctor(_: argparse.Namespace) -> int:
    emit(doctor_use_case())
    return 0


def _say(message: str) -> None:
    """Interactive prompt/status text goes to stderr, never stdout -- stdout
    is reserved for the single final `emit()` JSON result, same contract
    every other subcommand already honors."""
    print(message, file=sys.stderr)


def _ask(prompt: str) -> str:
    print(prompt, end="", file=sys.stderr)
    sys.stderr.flush()
    try:
        return input().strip()
    except EOFError as error:
        raise ValidationError(
            "sukuna-cli init aborted: stdin closed (EOF) during a prompt"
        ) from error


def _prompt_backend_choice() -> str | None:
    """Item 1 of 6. Only asked when both tmux and iTerm2 are installed;
    errors immediately if neither is; silently skipped (no question, no
    error) if exactly one is. The installed-backend precondition
    computation itself lives in `backend_prompt_precondition()` -- this
    function only handles display and input."""
    if backend_prompt_precondition() is not None:
        # Exactly one backend is installed: the precondition already
        # determined which one, but that confirmed value is not a
        # `preferred_backend` write (that setting only matters for the
        # tie-break case below), so the question is skipped outright.
        return None

    _say(
        "Both tmux and iTerm2 are installed. Which one should sukuna prefer when a session has "
        "both $TMUX and $ITERM_SESSION_ID set? This only affects that tie-break case."
    )
    _say("  1) tmux")
    _say("  2) iterm2")
    while True:
        choice = _ask("Choice [1/2, Enter to skip]: ")
        if choice == "":
            return None
        if choice == "1":
            return "tmux"
        if choice == "2":
            return "iterm2"
        _say("Please enter 1, 2, or press Enter to skip.")


def _prompt_active_pane_width() -> int | None:
    """Item 2 of 6: the width percentage applied to the pane being
    activated -- the newly spawned worker pane on `spawn` (horizontal
    split only), and the resize target on `focus` (the orchestrator's
    own pane by default, the named worker's pane with `--worker`)."""
    while True:
        raw = _ask("Active pane width percentage, 1-99 [Enter to skip]: ")
        if raw == "":
            return None
        try:
            value = int(raw)
        except ValueError:
            _say("Please enter a whole number, or press Enter to skip.")
            continue
        try:
            return check_active_pane_width(value)
        except ValidationError as error:
            _say(f"{error} Try again, or press Enter to skip.")


def _prompt_retention_days() -> int | None:
    """Item 6 of 6: how many days a CLOSED/FAILED(pane-less) registry record
    is kept before `sukuna spawn`'s automatic thinning or
    `sukuna reconcile --prune` retires it. Order relative to the other
    items is unspecified -- this prompt may appear in any position."""
    while True:
        raw = _ask("Registry retention, in days [Enter to skip]: ")
        if raw == "":
            return None
        try:
            value = int(raw)
        except ValueError:
            _say("Please enter a whole number, or press Enter to skip.")
            continue
        try:
            return check_retention_days(value)
        except ValidationError as error:
            _say(f"{error} Try again, or press Enter to skip.")


def _prompt_install_hooks() -> bool:
    """Item 3 of 6. The full `CONSENT_TEXT` explanation (the hook's
    behavior, fail-open) is shown before this question, not just the
    per-hook install status. The pending-matchers computation itself lives
    in `pending_hook_matchers()` -- this function only handles display and
    input."""
    pending = pending_hook_matchers()

    _say(CONSENT_TEXT)
    if pending:
        _say(f"Not yet installed: {', '.join(pending)}")
    else:
        _say("The hook is already installed; answering yes will leave it unchanged.")
    while True:
        answer = _ask(
            "Install this hook into ~/.claude/settings.json now? [y/N]: "
        ).lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        _say("Please answer y or n.")


def _prompt_should_focus_worker() -> bool | None:
    """Item 4 of 6: whether `sukuna focus --worker <name>` also switches
    tmux's real active pane to that worker (self-focus always does this
    unconditionally; this setting only governs the worker-focus path).
    `y/n` rather than `y/N` -- Enter means "leave the existing setting
    unchanged", not "no", so a `y/N` default would misleadingly suggest
    Enter overwrites an existing `true` with `false`."""
    while True:
        answer = _ask(
            "When focusing a worker (not the orchestrator itself), also switch tmux's real "
            "active pane to it? [y/n, Enter to skip]: "
        ).lower()
        if answer == "":
            return None
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        _say("Please answer y or n, or press Enter to skip.")


def _prompt_retry_or_skip() -> bool:
    """Follow-up question asked only after a failed build attempt (see
    `_run_tui_build_with_retry()`). Enter defaults to retrying, matching
    every other reprompt loop's "invalid input reprompts forever, Enter
    picks the forward-progress default" discipline."""
    while True:
        answer = _ask("Retry the build, or skip it? [R/s, Enter to retry]: ").lower()
        if answer in ("", "r", "retry"):
            return True
        if answer in ("s", "skip"):
            return False
        _say("Please answer r or s, or press Enter to retry.")


def _run_tui_build_with_retry() -> None:
    """Runs `attempt_tui_build()`, offering unlimited retry/skip on
    failure. `tui_enabled` itself was already decided by the caller and
    does not change here regardless of outcome (see the card's state
    transition table): skipping just means BUILT never happens, not that
    the TUI opt-in is withdrawn."""
    while True:
        _say("Building the tree TUI bundle from the embedded source...")
        try:
            attempt_tui_build()
        except BackendError as error:
            _say(f"Building the tree TUI bundle failed: {error}")
            if _prompt_retry_or_skip():
                continue
            _say("Skipping the tree TUI bundle build for now.")
            return
        _say("Tree TUI bundle built successfully.")
        return


def _prompt_enable_tui() -> bool | None:
    """Item 5 of 6: opt in to `sukuna-cli tree --tui`. Always asked (unlike
    item 1, there is no "obvious answer" case to skip) -- current Node.js
    and npm detection status is shown first via `tui_node_status()`/
    `npm_status_message()` so the user can judge feasibility themselves;
    answering yes still works even when Node is currently missing/too old,
    since `--tui` re-checks Node at invocation time regardless (the
    environment can change between `init` and actual use). Enter defaults
    to enabling: the consent flow itself stays interactive/required, but a
    new user who just presses Enter opts in, resolving the tension between
    "TUI should be the default" and the `tui_enabled` consent gate without
    making the setting non-interactive. A user who ran `init` before Enter
    defaulted to enabling keeps their prior answer unless they run `init`
    again.

    A `y`/`yes`/Enter answer that also satisfies `should_run_tui_build()`
    (npm found, source embedded, no usable bundle yet) immediately builds
    the TUI bundle -- the item count does not grow, but a "yes" answer can
    now take longer than before."""
    _say(TUI_CONSENT_TEXT)
    _say(tui_node_status())
    _say(npm_status_message())
    while True:
        answer = _ask(
            "Enable the interactive tree TUI (`sukuna-cli tree --tui`)? [Y/n, Enter to enable]: "
        ).lower()
        if answer in ("", "y", "yes"):
            if should_run_tui_build(tui_enabled_answer=True):
                _run_tui_build_with_retry()
            return True
        if answer in ("n", "no"):
            return False
        _say("Please answer y or n, or press Enter to enable.")


def _run_init_prompts() -> InitDecisions:
    if not sys.stdin.isatty():
        raise ValidationError(
            "sukuna-cli init requires an interactive terminal (stdin is not a TTY)"
        )
    preferred_backend = _prompt_backend_choice()
    active_pane_width = _prompt_active_pane_width()
    install_hooks = _prompt_install_hooks()
    should_focus_worker = _prompt_should_focus_worker()
    tui_enabled = _prompt_enable_tui()
    retention_days = _prompt_retention_days()
    return InitDecisions(
        install_hooks=install_hooks,
        active_pane_width=active_pane_width,
        preferred_backend=preferred_backend,
        should_focus_worker=should_focus_worker,
        tui_enabled=tui_enabled,
        retention_days=retention_days,
    )


def handle_init(_: argparse.Namespace) -> int:
    decisions = _run_init_prompts()
    emit(init_use_case(decisions))
    return 0


if TYPE_CHECKING:
    _SubParsers = argparse._SubParsersAction[argparse.ArgumentParser]


def _add_doctor_subcommand(subcommands: _SubParsers) -> None:
    doctor = subcommands.add_parser("doctor", help="check required local executables")
    doctor.set_defaults(handler=handle_doctor)


def _add_tree_subcommand(subcommands: _SubParsers) -> None:
    tree = subcommands.add_parser(
        "tree",
        help="show workers nested by parent worker, grouped by spawning session at the root",
    )
    tree.add_argument(
        "--limit",
        type=int,
        default=5,
        help=(
            "max number of root-level parent groups shown, ranked by each "
            "group's most recently updated direct member (default: 5; a "
            "shown group's members are never truncated); ignored when the "
            "TUI launches, since the TUI always shows every group "
            "(launch_tree_tui_use_case takes no limit argument at all)"
        ),
    )
    tree.add_argument(
        "--since",
        help=(
            "only include parent groups whose most recently updated direct "
            "member is at or after this local datetime (inclusive)"
        ),
    )
    tree.add_argument(
        "--to",
        help=(
            "only include parent groups whose most recently updated direct "
            "member is at or before this local datetime (inclusive)"
        ),
    )
    tree.add_argument(
        "--resumable-only",
        action="store_true",
        help=(
            "keep only workers whose worktree is the directory this command "
            "runs from (resumable via `claude --resume`), plus any ancestor "
            "needed to reach one; a parent group left with no such worker "
            "is dropped entirely"
        ),
    )
    mode = tree.add_mutually_exclusive_group()
    mode.add_argument(
        "--tui",
        action="store_true",
        help=(
            "force the interactive Node.js tree TUI -- requires an interactive "
            "terminal and the tui_enabled setting (see `sukuna-cli init`); with "
            "neither --tui nor --text, this launches automatically once "
            "tui_enabled is on and the terminal/Node.js requirements are met, "
            "falling back to text silently otherwise"
        ),
    )
    mode.add_argument(
        "--text",
        action="store_true",
        help=(
            "force static text output, overriding tui_enabled and the "
            "TUI auto-detection"
        ),
    )
    tree.set_defaults(handler=handle_tree)


def _add_init_subcommand(subcommands: _SubParsers) -> None:
    init = subcommands.add_parser(
        "init",
        help=(
            "interactively choose a preferred terminal backend, an active pane width, whether "
            "to install sukuna's resize hooks into ~/.claude/settings.json, whether worker "
            "focus should also switch tmux's real active pane, whether to enable the "
            "interactive tree TUI, and the registry retention period in days -- requires a "
            "TTY, takes no arguments"
        ),
    )
    init.set_defaults(handler=handle_init)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="sukuna-cli")
    result.add_argument(
        "--registry", help="override registry path (for testing or isolation)"
    )
    subcommands = result.add_subparsers(dest="operation", required=True)

    _add_doctor_subcommand(subcommands)
    _add_tree_subcommand(subcommands)
    _add_init_subcommand(subcommands)
    return result


def main_cli(argv: Sequence[str] | None = None) -> int:
    return _run(parser, argv, help_when_no_args=True)


if __name__ == "__main__":
    raise SystemExit(main_cli())

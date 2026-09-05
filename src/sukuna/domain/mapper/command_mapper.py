"""Build the only shell command executed in a worker pane."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from ...errors import ValidationError

_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")


def validate_role(role: str) -> str:
    if not _ROLE_RE.fullmatch(role):
        raise ValidationError("role must use lowercase letters, numbers, and hyphens")
    return role


def validate_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise ValidationError(
            "worker name must use lowercase letters, numbers, and hyphens"
        )
    return name


def _shell_wrap(shell: str, inner: str) -> str:
    """Wrap `inner` in `<shell> -lc` so shell operators (`&&`) and the
    `cd` builtin are interpreted -- iTerm2's Custom Command execs the
    string directly, without a shell. `shell` is resolved by the caller
    (`infrastructure.terminal.resolver.resolve_login_shell()`) -- this
    mapper stays a pure string formatter and never touches the environment
    or PATH itself."""
    return f"{shlex.quote(shell)} -lc {shlex.quote(inner)}"


def worker_command(
    *, worktree: Path, name: str, shell: str, model: str | None = None
) -> str:
    """Return a safely quoted command; task text is never included here.
    `model` passes through to `claude` unvalidated -- any membership check
    happens upstream, in `usecase/spawn.py`'s `spawn_many()` (opt-in, only
    when `$ANTHROPIC_API_KEY` is set). `model=None` omits `--model`
    entirely."""
    validate_name(name)
    parts = f"claude -n {shlex.quote(name)}"
    if model is not None:
        parts += f" --model {shlex.quote(model)}"
    return _shell_wrap(shell, f"cd {shlex.quote(str(worktree))} && exec {parts}")


def resume_command(*, worktree: Path, name: str, shell: str) -> str:
    """Reopen a pane-less worker via `claude --resume <name>`; takes no
    `model` -- `--resume` restores the session's original model
    automatically (https://code.claude.com/docs/en/sessions)."""
    validate_name(name)
    inner = (
        f"cd {shlex.quote(str(worktree))} && exec claude --resume {shlex.quote(name)}"
    )
    return _shell_wrap(shell, inner)

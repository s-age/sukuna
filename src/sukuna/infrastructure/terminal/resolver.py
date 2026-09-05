"""Which terminal backend to use, resolved from the environment or a pane ref."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ...errors import ValidationError
from .. import settings as settings_infra

_ITERM_BUNDLE_PATHS = (
    Path("/Applications/iTerm.app"),
    Path.home() / "Applications" / "iTerm.app",
)

_KNOWN_SAFE_LOGIN_SHELLS = {"zsh", "bash", "dash", "sh"}


def resolve_login_shell() -> str:
    """Which shell to wrap a worker's startup command in, as `<shell> -lc
    '...'`, so it reads the same login-init files (`.zprofile`, `.bashrc`,
    ...) a real login shell would -- needed on every backend, since neither
    tmux nor a Custom Command execs through a real login shell on its own.
    `$SHELL` is trusted outright when it names a known-safe shell:
    fish's `&&` support is version-dependent and
    tcsh's `-l` only takes effect as a lone argv[0] flag, so either as `$SHELL`
    would silently fail to become a login shell via `-lc`. Anything else
    (including an empty/unset `$SHELL`) falls back to the first of
    zsh/bash/`/bin/sh` found on PATH."""
    shell_env = os.environ.get("SHELL", "")
    if shell_env and Path(shell_env).name in _KNOWN_SAFE_LOGIN_SHELLS:
        return shell_env
    return shutil.which("zsh") or shutil.which("bash") or "/bin/sh"


def detect_backend() -> str:
    """No `--backend` flag: which terminal to use is a property of the
    environment sukuna is running in, not a caller choice. When only one of
    $TMUX/$ITERM_SESSION_ID is set, that one wins outright. When both are
    set, `preferred_backend` (written by `sukuna init`'s interactive
    prompt) tie-breaks; tmux remains the default when `preferred_backend`
    is unset or itself `"tmux"` (see CLAUDE.md's iTerm2 pitfalls)."""
    tmux_set = bool(os.environ.get("TMUX"))
    iterm_set = bool(os.environ.get("ITERM_SESSION_ID"))
    if tmux_set and iterm_set:
        if settings_infra.load_preferred_backend() == "iterm2":
            return "iterm2"
        return "tmux"
    if tmux_set:
        return "tmux"
    if iterm_set:
        return "iterm2"
    raise ValidationError(
        "no supported terminal detected: neither $TMUX nor $ITERM_SESSION_ID is set"
    )


def tmux_installed() -> bool:
    """Whether the `tmux` executable is on PATH -- distinct from (and
    checked independently of) `detect_backend()`'s $TMUX environment-variable
    check: this asks "could this session use tmux at all", not "is this
    session currently in one"."""
    return shutil.which("tmux") is not None


def iterm2_installed() -> bool:
    """Whether iTerm2 appears to be installed: a current $ITERM_SESSION_ID
    is sufficient proof on its own (a running session implies an install),
    otherwise fall back to checking the standard bundle install locations.
    Distinct from (and checked independently of) `detect_backend()`'s
    $ITERM_SESSION_ID check for the same reason as `tmux_installed()`."""
    if os.environ.get("ITERM_SESSION_ID"):
        return True
    return any(path.exists() for path in _ITERM_BUNDLE_PATHS)


def default_it2run_path() -> Path:
    """Where `it2run` lives inside an iTerm2.app bundle, derived from
    whichever of `_ITERM_BUNDLE_PATHS` exists on disk (first match wins) so
    this stays in sync with `iterm2_installed()`. Falls back to the first
    candidate when none exist."""
    for bundle in _ITERM_BUNDLE_PATHS:
        if bundle.exists():
            return bundle / "Contents" / "Resources" / "it2run"
    return _ITERM_BUNDLE_PATHS[0] / "Contents" / "Resources" / "it2run"


def infer_backend(pane_ref: str) -> str:
    """tmux pane ids are always `%<digits>`; iTerm2 session ids are always a
    UUID. The shape alone is enough to route `close` correctly without
    persisting which backend created a worker."""
    return "tmux" if pane_ref.startswith("%") else "iterm2"


def orchestrator_pane_ref(backend: str) -> str | None:
    """The pane this CLI is running in, if any (used to anchor the first
    split when no prior worker in this group recorded a pane yet)."""
    if backend == "tmux":
        # tmux sets this in every pane's environment; no parsing needed.
        return os.environ.get("TMUX_PANE") or None
    # `$ITERM_SESSION_ID` is `<window><tab><pane>:<uuid>`; iTerm2's API
    # wants the bare uuid.
    raw = os.environ.get("ITERM_SESSION_ID")
    if not raw:
        return None
    return raw.rsplit(":", maxsplit=1)[-1] or None

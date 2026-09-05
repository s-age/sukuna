"""`sukuna-cli init`: writes already-decided values for `install_hooks`,
`active_pane_width`, `preferred_backend`, `should_focus_worker`,
`tui_enabled`, and `retention_days` (each independently optional) into
setting.toml. The interactive prompt loop and its UI text live in the
CLI/presentation layers, not here."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.service.retention import validate_retention_days
from ..domain.service.spawn_placement import validate_active_pane_width
from ..domain.service.tui_build import (
    should_attempt_tui_build as _should_attempt_tui_build,
)
from ..errors import BackendError, ValidationError
from ..infrastructure import claude_settings, node_runtime, npm_runtime
from ..infrastructure import settings as settings_infra
from ..infrastructure.settings import (
    validate_preferred_backend,
    validate_should_focus_worker,
    validate_tui_enabled,
)
from ..infrastructure.terminal import resolver
from ..infrastructure.tui import launcher as tui_launcher
from . import tui_bundle


def check_active_pane_width(value: object) -> int:
    """Thin usecase-layer entry point so `cli_human.py`'s reprompt loop can
    validate a candidate `active_pane_width` without importing
    `infrastructure` or `domain` itself."""
    return validate_active_pane_width(value)


def check_retention_days(value: object) -> int:
    """Thin usecase-layer entry point, same shape as
    `check_active_pane_width()`, for `retention_days`."""
    return validate_retention_days(value)


def tui_node_status() -> str:
    """Thin usecase-layer pass-through window onto
    `node_runtime.node_status_message()`, same shape as
    `check_active_pane_width()`."""
    return node_runtime.node_status_message()


def npm_status_message() -> str:
    """Thin usecase-layer pass-through onto
    `npm_runtime.npm_status_message()`, same shape as `tui_node_status()`."""
    return npm_runtime.npm_status_message()


def should_run_tui_build(*, tui_enabled_answer: bool) -> bool:
    """Gathers the remaining three inputs `should_attempt_tui_build` needs
    and delegates the decision to it -- same orchestration shape as
    `tree.py::should_auto_launch_tui()`."""
    return _should_attempt_tui_build(
        tui_enabled_answer=tui_enabled_answer,
        npm_available=npm_runtime.npm_available(),
        tui_source_available=npm_runtime.tui_source_available(),
        bundle_already_present=tui_bundle.tui_bundle_available(),
    )


def attempt_tui_build() -> None:
    """Builds the TUI bundle from the package-embedded `_tui_source` into a
    fresh temporary directory (never into site-packages, which may be
    read-only) and installs the result into the user's state dir. Any
    failure along the way propagates as `BackendError` for the caller to
    handle (retry/skip prompting, in `cli_human.py`)."""
    with tempfile.TemporaryDirectory() as workdir:
        source_dir = Path(workdir) / "tui-source"
        npm_runtime.copy_tui_source(source_dir)
        npm_runtime.run_npm_ci(source_dir)
        built = npm_runtime.run_npm_build(source_dir)
        fingerprint = npm_runtime.embedded_tui_source_fingerprint()
        if fingerprint is None:
            raise BackendError("tui source disappeared during build")
        tui_launcher.install_bundle_from_build(built, fingerprint=fingerprint)


def backend_prompt_precondition() -> str | None:
    """Item 1's precondition: whether the installed-backend status alone
    decides the answer. Returns `ValidationError` if neither tmux nor
    iTerm2 is installed; the installed backend's name if exactly one is
    installed (not to be written as `preferred_backend`); or `None` if
    both are installed (ask the tie-break question)."""
    tmux_ok = resolver.tmux_installed()
    iterm_ok = resolver.iterm2_installed()
    if not tmux_ok and not iterm_ok:
        raise ValidationError(
            "sukuna requires tmux or iTerm2 to be installed; neither was detected on this machine"
        )
    if tmux_ok and iterm_ok:
        return None
    return "tmux" if tmux_ok else "iterm2"


def pending_hook_matchers(settings_json_path: Path | None = None) -> list[str]:
    """Item 3's precondition: which hook matchers `install_hooks()`
    would newly add, without writing anything."""
    settings_json_path = (
        settings_json_path
        if settings_json_path is not None
        else claude_settings.claude_settings_path()
    )
    document = claude_settings.read_settings_document(settings_json_path)
    _, hook_summary = claude_settings.merge_hooks(document)
    return sorted(matcher for matcher, would_add in hook_summary.items() if would_add)


@dataclass
class InitDecisions:
    """The already-decided answer to each of the six independently-gated
    items (module docstring) -- what `cli_human.py`'s interactive prompt loop
    produces and `init()` consumes."""

    install_hooks: bool
    active_pane_width: int | None
    preferred_backend: str | None
    should_focus_worker: bool | None = None
    tui_enabled: bool | None = None
    retention_days: int | None = None


def _validate_decisions(decisions: InitDecisions) -> InitDecisions:
    """Validates every item before writing any of them, so a bad value
    can't leave setting.toml partially written."""
    active_pane_width = decisions.active_pane_width
    if active_pane_width is not None:
        active_pane_width = validate_active_pane_width(active_pane_width)
    preferred_backend = decisions.preferred_backend
    if preferred_backend is not None:
        preferred_backend = validate_preferred_backend(preferred_backend)
    should_focus_worker = decisions.should_focus_worker
    if should_focus_worker is not None:
        should_focus_worker = validate_should_focus_worker(should_focus_worker)
    tui_enabled = decisions.tui_enabled
    if tui_enabled is not None:
        tui_enabled = validate_tui_enabled(tui_enabled)
    retention_days = decisions.retention_days
    if retention_days is not None:
        retention_days = validate_retention_days(retention_days)
    return InitDecisions(
        install_hooks=decisions.install_hooks,
        active_pane_width=active_pane_width,
        preferred_backend=preferred_backend,
        should_focus_worker=should_focus_worker,
        tui_enabled=tui_enabled,
        retention_days=retention_days,
    )


def _apply_decisions(
    decisions: InitDecisions,
    *,
    settings_json_path: Path,
    setting_toml_path: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "settings_json_path": str(settings_json_path)}
    if decisions.install_hooks:
        result["installed_hooks"] = claude_settings.install_hooks(settings_json_path)
    if decisions.active_pane_width is not None:
        settings_infra.write_active_pane_width(
            decisions.active_pane_width, setting_toml_path
        )
        result["active_pane_width"] = decisions.active_pane_width
        result["setting_toml_path"] = str(setting_toml_path)
    if decisions.preferred_backend is not None:
        settings_infra.write_preferred_backend(
            decisions.preferred_backend, setting_toml_path
        )
        result["preferred_backend"] = decisions.preferred_backend
        result["setting_toml_path"] = str(setting_toml_path)
    if decisions.should_focus_worker is not None:
        settings_infra.write_should_focus_worker(
            decisions.should_focus_worker, setting_toml_path
        )
        result["should_focus_worker"] = decisions.should_focus_worker
        result["setting_toml_path"] = str(setting_toml_path)
    if decisions.tui_enabled is not None:
        settings_infra.write_tui_enabled(decisions.tui_enabled, setting_toml_path)
        result["tui_enabled"] = decisions.tui_enabled
        result["setting_toml_path"] = str(setting_toml_path)
    if decisions.retention_days is not None:
        settings_infra.write_retention_days(decisions.retention_days, setting_toml_path)
        result["retention_days"] = decisions.retention_days
        result["setting_toml_path"] = str(setting_toml_path)
    return result


def init(
    decisions: InitDecisions,
    *,
    settings_json_path: Path | None = None,
    setting_toml_path: Path | None = None,
) -> dict[str, Any]:
    settings_json_path = (
        settings_json_path
        if settings_json_path is not None
        else claude_settings.claude_settings_path()
    )
    setting_toml_path = (
        setting_toml_path
        if setting_toml_path is not None
        else settings_infra.settings_path()
    )
    decisions = _validate_decisions(decisions)
    return _apply_decisions(
        decisions,
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

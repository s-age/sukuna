"""Read/write the optional global settings TOML file.

Lives next to `registry.json`, under the same XDG-overridable directory
(`sukuna_state_dir()`, imported from `registry.py`). Grain is global, not
per-role. Five keys -- `active_pane_width`, `preferred_backend`,
`should_focus_worker`, `tui_enabled`, `retention_days` -- each documented
on its own `load_*()`/`write_*()` pair below. TOML has no null literal:
an explicit `null` in the file is a parse error (`ValidationError`),
never a fallback to a key's default.
"""

from __future__ import annotations

import fcntl
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

import tomlkit
from tomlkit.exceptions import TOMLKitError

from ..domain.service.retention import DEFAULT_RETENTION_DAYS, validate_retention_days
from ..domain.service.spawn_placement import validate_active_pane_width
from ..errors import ValidationError
from .atomic_write import atomic_write
from .registry import sukuna_state_dir

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "validate_active_pane_width",
    "validate_retention_days",
]

DEFAULT_ACTIVE_PANE_WIDTH = 50
DEFAULT_SHOULD_FOCUS_WORKER = False
DEFAULT_TUI_ENABLED = False
VALID_BACKENDS = frozenset({"tmux", "iterm2"})

T = TypeVar("T")


@dataclass(frozen=True)
class _SettingKey(Generic[T]):
    """One TOML key's name, validator, and fallback."""

    name: str
    validate: Callable[[object], T]
    default: T


def settings_path() -> Path:
    return sukuna_state_dir() / "setting.toml"


def validate_preferred_backend(value: object) -> str:
    """`{"tmux", "iterm2"}` membership check, the same shape as
    `validate_active_pane_width()`."""
    if not isinstance(value, str) or value not in VALID_BACKENDS:
        raise ValidationError(
            f"preferred_backend must be one of {sorted(VALID_BACKENDS)}"
        )
    return value


def validate_should_focus_worker(value: object) -> bool:
    """Type check only, the same shape as `validate_preferred_backend()`."""
    if not isinstance(value, bool):
        raise ValidationError("should_focus_worker must be a boolean")
    return value


def validate_tui_enabled(value: object) -> bool:
    """Type check only, the same shape as `validate_should_focus_worker()`."""
    if not isinstance(value, bool):
        raise ValidationError("tui_enabled must be a boolean")
    return value


_ACTIVE_PANE_WIDTH: _SettingKey[int] = _SettingKey(
    name="active_pane_width",
    validate=validate_active_pane_width,
    default=DEFAULT_ACTIVE_PANE_WIDTH,
)
_PREFERRED_BACKEND: _SettingKey[str | None] = _SettingKey(
    name="preferred_backend",
    validate=validate_preferred_backend,
    default=None,
)
_SHOULD_FOCUS_WORKER: _SettingKey[bool] = _SettingKey(
    name="should_focus_worker",
    validate=validate_should_focus_worker,
    default=DEFAULT_SHOULD_FOCUS_WORKER,
)
_TUI_ENABLED: _SettingKey[bool] = _SettingKey(
    name="tui_enabled",
    validate=validate_tui_enabled,
    default=DEFAULT_TUI_ENABLED,
)
_RETENTION_DAYS: _SettingKey[int] = _SettingKey(
    name="retention_days",
    validate=validate_retention_days,
    default=DEFAULT_RETENTION_DAYS,
)


def _read_document(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except TOMLKitError as error:
        raise ValidationError(f"{path} is not valid TOML: {error}") from error


def _load_key(key: _SettingKey[T], path: Path | None) -> T:
    """Shared by every `load_*()` below: resolve the path, parse the
    document (or treat a missing file as empty), fall back to `key.default`
    when the key is absent, otherwise validate the stored value."""
    path = path if path is not None else settings_path()
    value = _read_document(path).get(key.name)
    if value is None:
        return key.default
    return key.validate(value)


def _write_key(key: _SettingKey[T], value: T, path: Path | None) -> None:
    """Shared by every `write_*()` below: read-modify-write the one key
    `key` owns through `tomlkit`, preserving every other key, comment, and
    formatting choice untouched, and creating the file (and its parent) if
    it does not exist yet. Holds a lock for the read-modify-write duration
    and writes through a temp file + atomic rename (same discipline as
    `registry.py`'s `_locked()` and `claude_settings.py`'s
    `install_hooks()`)."""
    path = path if path is not None else settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            document = _read_document(path)
            document[key.name] = value
            atomic_write(path, tomlkit.dumps(document), prefix=".setting-")
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_active_pane_width(path: Path | None = None) -> int:
    """Return the configured `active_pane_width`, always resolving to a
    concrete percentage: unset (no settings file, or the key absent) falls
    back to `DEFAULT_ACTIVE_PANE_WIDTH`. Raises `ValidationError` if the
    file exists but is not valid TOML, or the value fails the same
    range/type check as the spawn stdin JSON key."""
    return _load_key(_ACTIVE_PANE_WIDTH, path)


def write_active_pane_width(value: int, path: Path | None = None) -> None:
    """Write `active_pane_width` into the settings TOML file, preserving
    every other line untouched and creating the file if it does not exist
    yet."""
    value = validate_active_pane_width(value)
    _write_key(_ACTIVE_PANE_WIDTH, value, path)


def load_preferred_backend(path: Path | None = None) -> str | None:
    """Return the configured `preferred_backend`, or `None` if unset (no
    settings file, or the key absent). Raises `ValidationError` if the
    file exists but is not valid TOML, or the value is not one of
    `VALID_BACKENDS`."""
    return _load_key(_PREFERRED_BACKEND, path)


def write_preferred_backend(value: str, path: Path | None = None) -> None:
    """Write `preferred_backend` into the settings TOML file, preserving
    every other line untouched and creating the file if it does not exist
    yet -- same one-line-touch discipline as `write_active_pane_width()`."""
    value = validate_preferred_backend(value)
    _write_key(_PREFERRED_BACKEND, value, path)


def load_should_focus_worker(path: Path | None = None) -> bool:
    """Return the configured `should_focus_worker` (whether `--worker`
    focus also switches tmux's real active pane to the worker, on top of
    the width-change self-focus that always applies), always resolving
    to a concrete bool: unset (no settings file, or the key absent)
    falls back to `DEFAULT_SHOULD_FOCUS_WORKER`. Raises `ValidationError`
    if the file exists but is not valid TOML, or the value fails the
    boolean type check."""
    return _load_key(_SHOULD_FOCUS_WORKER, path)


def write_should_focus_worker(value: bool, path: Path | None = None) -> None:
    """Write `should_focus_worker` into the settings TOML file, preserving
    every other line untouched and creating the file if it does not exist
    yet -- same one-line-touch discipline as `write_active_pane_width()`."""
    value = validate_should_focus_worker(value)
    _write_key(_SHOULD_FOCUS_WORKER, value, path)


def load_tui_enabled(path: Path | None = None) -> bool:
    """Return the configured `tui_enabled` (gates `sukuna-cli tree
    --tui`), always resolving to a concrete bool: unset (no settings
    file, or the key absent) falls back to `DEFAULT_TUI_ENABLED`. Raises
    `ValidationError` if the file exists but is not valid TOML, or the
    value fails the boolean type check."""
    return _load_key(_TUI_ENABLED, path)


def write_tui_enabled(value: bool, path: Path | None = None) -> None:
    """Write `tui_enabled` into the settings TOML file, preserving every
    other line untouched and creating the file if it does not exist yet --
    same one-line-touch discipline as `write_should_focus_worker()`."""
    value = validate_tui_enabled(value)
    _write_key(_TUI_ENABLED, value, path)


def load_retention_days(path: Path | None = None) -> int:
    """Return the configured `retention_days`, always resolving to a
    concrete int: unset (no settings file, or the key absent) falls back
    to `DEFAULT_RETENTION_DAYS`. Raises `ValidationError` if the file
    exists but is not valid TOML, or the value fails
    `validate_retention_days()`."""
    return _load_key(_RETENTION_DAYS, path)


def write_retention_days(value: int, path: Path | None = None) -> None:
    """Write `retention_days` into the settings TOML file, preserving every
    other line untouched and creating the file if it does not exist yet --
    same one-line-touch discipline as `write_active_pane_width()`."""
    value = validate_retention_days(value)
    _write_key(_RETENTION_DAYS, value, path)

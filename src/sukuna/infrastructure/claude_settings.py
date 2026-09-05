"""Merge sukuna's hook entries into the user-level `~/.claude/settings.json`.

Owned by Claude Code, not sukuna: it carries other top-level keys
(`model`, `permissions`, ...) and other `hooks.PreToolUse` matcher groups
that a `sukuna-cli init` run must not disturb. Installing is idempotent:
append to an existing matcher group's `hooks` array, or add a new
matcher group.
"""

from __future__ import annotations

import copy
import fcntl
import json
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic import ValidationError as PydanticValidationError

from ..errors import ValidationError
from .atomic_write import atomic_write

HOOK_EVENT = "PreToolUse"

_SELF_FOCUS_COMMAND = (
    'node -e \'try { require("child_process").execFileSync('
    '"sukuna", ["focus"], {stdio: "ignore"}); } catch (e) {}\''
)

ASK_USER_QUESTION_HOOK_GROUP: dict[str, Any] = {
    "matcher": "AskUserQuestion",
    "hooks": [{"type": "command", "command": _SELF_FOCUS_COMMAND}],
}

HOOK_GROUPS_TO_INSTALL: tuple[dict[str, Any], ...] = (ASK_USER_QUESTION_HOOK_GROUP,)


def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def read_settings_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValidationError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise ValidationError(
            f"{path} must contain a JSON object at the top level, "
            f"got {type(document).__name__}"
        )
    return document


def _hook_entry_matches(existing: dict[str, Any], new: dict[str, Any]) -> bool:
    return existing.get("type") == new.get("type") and existing.get(
        "command"
    ) == new.get("command")


def _merge_matcher_group(
    event_groups: list[dict[str, Any]], new_group: dict[str, Any]
) -> bool:
    """Mutates `event_groups` in place. Returns whether anything was newly
    added (False means this exact hook command was already installed)."""
    for group in event_groups:
        if group.get("matcher") == new_group["matcher"]:
            hooks_list = group.setdefault("hooks", [])
            added = False
            for new_hook in new_group["hooks"]:
                if not any(
                    _hook_entry_matches(existing, new_hook) for existing in hooks_list
                ):
                    hooks_list.append(copy.deepcopy(new_hook))
                    added = True
            return added
    event_groups.append(copy.deepcopy(new_group))
    return True


class _HookCommandEntry(BaseModel):
    """One entry in a matcher group's `hooks` array, e.g.
    `{"type": "command", "command": "..."}`. Shape beyond "is an object" is
    intentionally unconstrained -- settings.json is not owned by sukuna and
    a `hooks` entry may carry keys sukuna does not know about."""

    model_config = ConfigDict(extra="allow")


class _MatcherGroup(BaseModel):
    """One `hooks.<event>` entry, e.g. `{"matcher": "Bash", "hooks": [...]}`.
    Unknown keys pass through untouched via `extra="allow"`."""

    model_config = ConfigDict(extra="allow")

    matcher: Any = None
    hooks: list[_HookCommandEntry] | None = None

    @field_validator("hooks", mode="before")
    @classmethod
    def _hooks_is_a_list_of_objects(cls, value: Any, info: ValidationInfo) -> Any:
        # `assert`, not `raise ValueError`/`TypeError`: pydantic before-validators
        # only turn a raised AssertionError or ValueError into part of the model's
        # ValidationError -- a raised TypeError propagates uncaught (verified).
        matcher = info.data.get("matcher")
        assert isinstance(value, list), (
            f'matcher group {matcher!r} "hooks" must be an array, '
            f"got {type(value).__name__}"
        )
        for entry in value:
            assert isinstance(entry, dict), (
                f'matcher group {matcher!r} "hooks" entries must be '
                f"objects, got {type(entry).__name__}"
            )
        return value


class _HooksSection(BaseModel):
    """The document's `hooks` object. Only `HOOK_EVENT`'s shape is
    validated; other event keys (`PostToolUse`, ...) pass through untouched
    via `extra="allow"`."""

    model_config = ConfigDict(extra="allow")

    event_groups: list[_MatcherGroup] | None = None

    @model_validator(mode="before")
    @classmethod
    def _rename_hook_event_key(cls, value: Any) -> Any:
        """Renames the incoming `HOOK_EVENT` JSON key to the literal
        field name `event_groups` before validation."""
        if isinstance(value, dict) and HOOK_EVENT in value:
            value = dict(value)
            value["event_groups"] = value.pop(HOOK_EVENT)
        return value

    @field_validator("event_groups", mode="before")
    @classmethod
    def _event_groups_is_a_list_of_objects(cls, value: Any) -> Any:
        assert isinstance(value, list), (
            f'"hooks.{HOOK_EVENT}" must be an array, got {type(value).__name__}'
        )
        for group in value:
            assert isinstance(group, dict), (
                f'each "hooks.{HOOK_EVENT}" entry must be an object, '
                f"got {type(group).__name__}"
            )
        return value


class _SettingsDocument(BaseModel):
    """Top-level settings.json shape sukuna cares about. Every other
    top-level key (`model`, `permissions`, ...) passes through untouched via
    `extra="allow"`."""

    model_config = ConfigDict(extra="allow")

    hooks: _HooksSection | None = None

    @field_validator("hooks", mode="before")
    @classmethod
    def _hooks_is_an_object(cls, value: Any) -> Any:
        assert isinstance(value, dict), (
            f'"hooks" must be an object, got {type(value).__name__}'
        )
        return value


def _validate_hooks_structure(document: dict[str, Any]) -> None:
    """settings.json is not owned by sukuna; a malformed `hooks` section
    is reported for manual fixing. Field validators above only run when
    the corresponding key is present in the input (pydantic skips
    validation for keys that fall back to their default) -- an absent
    `hooks` or `hooks.<event>` key is accepted."""
    try:
        _SettingsDocument.model_validate(document)
    except PydanticValidationError as error:
        raise ValidationError(
            f"settings.json's hooks structure is invalid (manual fix required): {error}"
        ) from error


def merge_hooks(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bool]]:
    """Pure merge, no disk access: returns `(new_document, summary)` where
    `summary` maps each matcher name to whether it was newly added by this
    call. Used both to install for real and to preview a dry run."""
    _validate_hooks_structure(document)
    document = copy.deepcopy(document)
    event_groups = document.setdefault("hooks", {}).setdefault(HOOK_EVENT, [])
    summary = {
        group["matcher"]: _merge_matcher_group(event_groups, group)
        for group in HOOK_GROUPS_TO_INSTALL
    }
    return document, summary


def install_hooks(path: Path) -> dict[str, bool]:
    """Read-merge-write `path`, holding a lock for the duration and
    delegating the write itself to `atomic_write()` (shared with the sukuna
    registry, applied here to a file sukuna does not own)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            document = read_settings_document(path)
            new_document, summary = merge_hooks(document)
            if any(summary.values()):
                content = json.dumps(new_document, indent=2, ensure_ascii=False) + "\n"
                atomic_write(path, content, prefix=".settings-")
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return summary

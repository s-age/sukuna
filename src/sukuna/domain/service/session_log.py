"""Pure business rules for locating a worker's Claude Code session
transcript: reproducing Claude Code's undocumented `~/.claude/projects/
<encodedDir>` naming, and matching a worker name against a jsonl's
leading `custom-title` entry. No filesystem access and no `json` import
here (domain boundary policy, `.claude/CLAUDE.md`) -- lines are read and
JSON-decoded by `infrastructure/claude_projects.py`, which hands this
module already-parsed entries."""

from __future__ import annotations

import re
from typing import Any

_MAX_SANITIZED_LENGTH = 200
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")
_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"

_SYNTHETIC_MODEL_SENTINEL = "<synthetic>"

# `claude --permission-mode`'s accepted values as of claude 2.1.257.
PERMISSION_MODE_CHOICES = (
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "manual",
    "dontAsk",
    "plan",
)


def encode_project_dir_name(cwd: str) -> str:
    """Reproduce Claude Code's `~/.claude/projects/<dir>` naming: every
    non-alphanumeric character becomes `-`; names over
    `_MAX_SANITIZED_LENGTH` fall back to a truncate+hash suffix.
    Reverse-engineered from perclst's `sanitizeDirName()`
    (`claudeSessions.ts:50-56`) and confirmed against this worktree's own
    `~/.claude/projects` entry -- unverified for the truncate+hash branch,
    since no worktree path here has been observed to exceed the
    threshold."""
    sanitized = _NON_ALNUM.sub("-", cwd)
    if len(sanitized) <= _MAX_SANITIZED_LENGTH:
        return sanitized
    digest = abs(_djb2(cwd))
    return f"{sanitized[:_MAX_SANITIZED_LENGTH]}-{_to_base36(digest)}"


def _djb2(raw: str) -> int:
    """32-bit signed djb2, matching the JS reference's `(hash << 5) - hash
    + charCode | 0` (ToInt32) semantics."""
    hash_value = 0
    for char in raw:
        hash_value = ((hash_value << 5) - hash_value + ord(char)) & 0xFFFFFFFF
    if hash_value >= 0x80000000:
        hash_value -= 0x100000000
    return hash_value


def _to_base36(value: int) -> str:
    if value == 0:
        return "0"
    digits: list[str] = []
    remaining = value
    while remaining:
        remaining, rem = divmod(remaining, 36)
        digits.append(_BASE36_DIGITS[rem])
    return "".join(reversed(digits))


def find_custom_title_match(entries: list[Any], name: str) -> bool:
    """True if any of `entries` (a jsonl file's leading lines, already
    JSON-decoded by the infrastructure caller -- non-JSON or non-object
    lines are its job to skip, not this function's) is a `custom-title`
    entry whose `customTitle` equals `name` -- the only name-to-session-file
    link `claude -n <name>` leaves behind."""
    for entry in entries:
        if (
            isinstance(entry, dict)
            and entry.get("type") == "custom-title"
            and entry.get("customTitle") == name
        ):
            return True
    return False


def extract_permission_mode(entry: Any) -> str | None:
    """The raw `permissionMode` value of a `type: "permission-mode"`
    jsonl entry, unvalidated -- `validate_permission_mode()` below is a
    separate step so a caller scanning many entries can defer validation
    until it has settled on the last-seen raw value."""
    if not isinstance(entry, dict) or entry.get("type") != "permission-mode":
        return None
    value = entry.get("permissionMode")
    return value if isinstance(value, str) else None


def validate_permission_mode(value: str | None) -> str | None:
    """`None` for anything outside `PERMISSION_MODE_CHOICES`, including the
    `"default"` sentinel (meaning "no explicit mode was set that turn")
    and any value from a future CLI version
    this list hasn't been updated for -- both degrade the same way a
    caller degrades a missing entry: flag omission."""
    return value if value in PERMISSION_MODE_CHOICES else None


def extract_model(entry: Any) -> str | None:
    """The raw `message.model` value of a jsonl entry, unvalidated like
    `worker_command()`'s own `model` -- except for `"<synthetic>"`, an
    internal marker Claude Code writes for turns that never called the
    API (all-zero token usage, empty `stop_sequence`): that value never
    represents a model actually in use, so it is treated the same as a
    missing one rather than passed through."""
    if not isinstance(entry, dict):
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    model = message.get("model")
    if not isinstance(model, str) or model == _SYNTHETIC_MODEL_SENTINEL:
        return None
    return model


def resolve_respawn_model(
    jsonl_model: str | None, spawn_model: str | None
) -> str | None:
    """Tier 2 of the model fallback chain (card A3BFA355 §3): the
    jsonl-derived last-actual-value wins when available; `spawn_model`
    (`WorkerRecord.model`) is used only when the jsonl yielded nothing.
    Arbitrating between two recorded sources of truth is domain policy,
    not usecase orchestration -- kept out of `respawn.py` for that reason."""
    return jsonl_model if jsonl_model is not None else spawn_model

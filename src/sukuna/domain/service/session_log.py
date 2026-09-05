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

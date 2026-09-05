"""Static validation for one element of a spawn batch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...errors import ValidationError
from ..mapper.command_mapper import validate_name, validate_role
from .spawn_placement import validate_active_pane_width, worker_name


@dataclass(frozen=True)
class SpawnSpec:
    """One validated, resolved element of a spawn batch -- the typed
    product of `preflight_one_spec()`. Every field is required (no
    defaults); the single construction site below fills all seven."""

    role: str
    repo: Path
    worktree: Path
    goal: str | None
    parent_worker: str | None
    active_pane_width: int
    model: str | None


_KNOWN_KEYS = {
    "role",
    "repo",
    "worktree",
    "goal",
    "parent_worker",
    "active_pane_width",
    "model",
}


def _check_spec_shape(raw_spec: dict[str, object]) -> None:
    """First-pass structural checks: no keys the batch doesn't know about,
    and every required key present. Raises before anything reads a value,
    so a shape problem is reported without also tripping a `KeyError`."""
    unknown = set(raw_spec) - _KNOWN_KEYS
    if unknown:
        raise ValidationError(f"unknown key(s): {', '.join(sorted(unknown))}")
    missing = [key for key in ("role", "repo", "worktree") if key not in raw_spec]
    if missing:
        raise ValidationError(f"missing required key(s): {', '.join(missing)}")


def _resolve_repo_and_worktree(
    raw_spec: dict[str, object],
    resolve_existing_directory: Callable[[str], Path | None],
) -> tuple[Path, Path]:
    """`resolve_existing_directory` performs the actual filesystem I/O
    (`expanduser()`/`resolve()`/`is_dir()`) -- this domain function must not
    touch the filesystem itself (layer responsibility policy), so the
    caller injects `infrastructure.filesystem.resolve_existing_directory`
    and this function only turns its `None` result into a `ValidationError`."""
    raw_repo, raw_worktree = raw_spec["repo"], raw_spec["worktree"]
    if not isinstance(raw_repo, str) or not isinstance(raw_worktree, str):
        raise ValidationError("repo and worktree must be strings")
    if not raw_repo or not raw_worktree:
        raise ValidationError("repo and worktree must be non-empty paths")
    repo = resolve_existing_directory(raw_repo)
    worktree = resolve_existing_directory(raw_worktree)
    if repo is None or worktree is None:
        raise ValidationError("repo and worktree must be existing directories")
    return repo, worktree


def _validate_role_value(role: object) -> str:
    if not isinstance(role, str):
        raise ValidationError("role must be a string")
    return validate_role(role)


def _validate_parent_worker_value(parent_worker: object) -> str | None:
    if parent_worker is None:
        return None
    if not isinstance(parent_worker, str):
        raise ValidationError("parent_worker must be a string")
    return validate_name(parent_worker)


def _validate_goal_value(goal: object) -> str | None:
    if goal is not None and not isinstance(goal, str):
        raise ValidationError("goal must be a string")
    return goal


def _validate_model_value(model: object) -> str | None:
    if model is None:
        return None
    if not isinstance(model, str):
        raise ValidationError("model must be a string")
    # Empty/whitespace-only rejection ONLY -- `""` demonstrably lands as
    # `claude --model ''` in the spawn command when no
    # `$ANTHROPIC_API_KEY` catalog check runs. Broader lexical/format
    # validation of `model` stays out.
    if not model.strip():
        raise ValidationError("model must not be empty")
    return model


def _resolve_active_pane_width(
    raw_spec: dict[str, object], *, default_active_pane_width: int
) -> int:
    if "active_pane_width" in raw_spec and raw_spec["active_pane_width"] is not None:
        return validate_active_pane_width(raw_spec["active_pane_width"])
    # Key absent or explicit `null` both resolve to the same default --
    # there is no per-call "leave the pane untouched" escape hatch.
    return default_active_pane_width


def _validate_generated_name(
    repo: Path, parent_session_id: str | None, role: str
) -> None:
    """Double-checks the name `spawn_many()` will later generate for this
    element (`worker_name()` already sanitizes both the repo slug and the
    session-id token to ASCII, so this should never actually fire -- a
    defense-in-depth guard against that sanitization regressing). The
    real `ordinal` isn't known here (`registry.list()` isn't touched in
    this pass); `ordinal=1` is a placeholder -- validity against
    `_NAME_RE` doesn't depend on the ordinal's value, only on it being all
    digits, and the fixed parts (`"ccw-" + slug<=32 + token<=8 +
    role<=32` + 3 hyphens = 79 chars) leave room for far more ordinal
    digits than any real batch will ever reach under the 96-char
    limit."""
    candidate_name = worker_name(repo, parent_session_id, role, 1)
    try:
        validate_name(candidate_name)
    except ValidationError as name_error:
        raise ValidationError(
            f"generated worker name {candidate_name!r} is invalid: {name_error}"
        ) from name_error


def preflight_one_spec(
    raw_spec: object,
    *,
    parent_session_id: str | None,
    default_active_pane_width: int,
    resolve_existing_directory: Callable[[str], Path | None],
) -> SpawnSpec:
    """Statically validate one element of a spawn batch (JSON shape,
    unknown keys, required keys, existing-directory check,
    `validate_role()`/`validate_name()`) and resolve it to a `SpawnSpec`
    -- no registry or backend operation runs here. Raises
    `ValidationError` on the first violation; the caller (the batch loop
    in `usecase.spawn.preflight_spawn_specs()`) is responsible for
    numbering the failing element and aggregating problems across the
    batch. Each sub-check lives in its own `_validate_*`/`_resolve_*`
    helper."""
    if not isinstance(raw_spec, dict):
        raise ValidationError("element must be a JSON object")
    _check_spec_shape(raw_spec)
    repo, worktree = _resolve_repo_and_worktree(raw_spec, resolve_existing_directory)
    role = _validate_role_value(raw_spec["role"])
    parent_worker = _validate_parent_worker_value(raw_spec.get("parent_worker"))
    goal = _validate_goal_value(raw_spec.get("goal"))
    model = _validate_model_value(raw_spec.get("model"))
    active_pane_width = _resolve_active_pane_width(
        raw_spec, default_active_pane_width=default_active_pane_width
    )
    _validate_generated_name(repo, parent_session_id, role)
    return SpawnSpec(
        role=role,
        repo=repo,
        worktree=worktree,
        goal=goal,
        parent_worker=parent_worker,
        active_pane_width=active_pane_width,
        model=model,
    )

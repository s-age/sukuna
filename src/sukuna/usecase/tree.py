"""Render the worker registry as a nested, human-readable tree, or launch
the interactive Node.js TUI over that same selected tree."""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict, Unpack

from ..domain.entity.worker_record import WorkerRecord
from ..domain.service.tree_mode import should_launch_tui as _should_launch_tui
from ..domain.service.worker_tree import (
    attach_resumable_flags,
    attach_session_log_paths,
    build_tree,
    prune_to_resumable,
    resolve_group_session_log_paths,
    select_parent_groups,
)
from ..errors import ValidationError
from ..infrastructure import claude_projects as claude_projects_infra
from ..infrastructure import node_runtime as node_runtime_infra
from ..infrastructure import settings as settings_infra
from ..infrastructure.registry import Registry
from ..infrastructure.tui import launcher as tui_launcher
from ..presentation.tree_presenter import parse_local_datetime, render_compact_tree
from .tui_bundle import state_dir_bundle_usable, tui_bundle_available


class TreeFilters(TypedDict, total=False):
    """`tree()`/`tree_payload()`'s trailing keyword filters
    (`since`/`to`/`cwd`/`resumable_only`), bundled behind
    `**filters: Unpack[TreeFilters]`."""

    since: str | None
    to: str | None
    cwd: str | None
    resumable_only: bool


def _parse_range_arg(value: str | None, flag: str) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_local_datetime(value)
    except ValueError as error:
        raise ValidationError(
            f"{flag} must be a valid local datetime: {error}"
        ) from error


def _selected_tree(
    workers: list[WorkerRecord],
    *,
    limit: int | None,
    since: str | None,
    to: str | None,
) -> tuple[dict[str, list[Any]], int]:
    return select_parent_groups(
        build_tree(workers),
        limit=limit,
        since=_parse_range_arg(since, "--since"),
        to=_parse_range_arg(to, "--to"),
    )


def _apply_resumable(
    groups: dict[str, list[Any]],
    workers: list[WorkerRecord],
    *,
    cwd: str | None,
    resumable_only: bool,
) -> dict[str, list[Any]]:
    """`cwd=None` still attaches a `resumable` key to every node -- always
    `False`, since no worker's `worktree` is ever an empty string
    (rejected at spawn time) -- rather than leaving it unattached, so
    every node consistently carries the field."""
    by_name = {worker.name: worker for worker in workers}
    attach_resumable_flags(groups, by_name, cwd if cwd is not None else "")
    return prune_to_resumable(groups) if resumable_only else groups


def tree(
    registry: Registry,
    *,
    limit: int = 5,
    **filters: Unpack[TreeFilters],
) -> str:
    since = filters.get("since")
    to = filters.get("to")
    workers = registry.list()
    selected, hidden = _selected_tree(workers, limit=limit, since=since, to=to)
    selected = _apply_resumable(
        selected,
        workers,
        cwd=filters.get("cwd"),
        resumable_only=filters.get("resumable_only", False),
    )
    return render_compact_tree(selected, hidden_parent_groups=hidden)


def tree_payload(
    registry: Registry,
    *,
    limit: int | None = 5,
    **filters: Unpack[TreeFilters],
) -> dict[str, Any]:
    """Same selection as `tree()`, plus a `session_log_path` and a
    `resumable` key on every node, and a `group_session_log_paths` entry
    per top-level group key. Per-node `session_log_path` is a pure read
    of the value already cached on each `WorkerRecord`; `tree_payload()`
    itself never touches the filesystem for it. `resumable` compares
    each worker's `worktree` against `cwd` (`cwd=None` still attaches
    the field, always `False`). `resumable_only=True` prunes to
    resumable nodes plus any ancestor needed to reach them, which can
    also drop a top-level group key entirely. `group_session_log_paths`
    is a separate, filesystem-backed resolution, bounded to the groups
    that survive selection/pruning."""
    workers = registry.list()
    groups, hidden = _selected_tree(
        workers, limit=limit, since=filters.get("since"), to=filters.get("to")
    )
    session_log_path_by_name = {
        worker.name: worker.session_log_path for worker in workers
    }
    attach_session_log_paths(
        groups,
        {worker.name: worker.worktree for worker in workers},
        lambda _worktree, name: session_log_path_by_name.get(name),
    )
    groups = _apply_resumable(
        groups,
        workers,
        cwd=filters.get("cwd"),
        resumable_only=filters.get("resumable_only", False),
    )
    group_session_log_paths = resolve_group_session_log_paths(
        groups, claude_projects_infra.resolve_parent_session_log_path
    )
    return {
        "groups": groups,
        "hidden_parent_groups": hidden,
        "group_session_log_paths": group_session_log_paths,
    }


def should_auto_launch_tui(*, stdin_is_tty: bool, stdout_is_tty: bool) -> bool:
    """No `--tui`/`--text` flag given: gathers the two remaining
    auto-detection inputs (`tui_enabled`, Node/bundle availability) and
    hands all four to `should_launch_tui` -- pure orchestration, the
    branching logic itself lives in that domain function. TTY-ness is the
    caller's to gather, since it describes the CLI process's own stdio."""
    return _should_launch_tui(
        tui_enabled=settings_infra.load_tui_enabled(),
        stdin_is_tty=stdin_is_tty,
        stdout_is_tty=stdout_is_tty,
        node_and_bundle_ok=node_runtime_infra.node_satisfies_tui_minimum()
        and tui_bundle_available(),
    )


def launch_tree_tui(
    registry: Registry,
    *,
    since: str | None = None,
    to: str | None = None,
    cwd: str | None = None,
    resumable_only: bool = False,
) -> int:
    if not settings_infra.load_tui_enabled():
        raise ValidationError(
            "sukuna-cli tree --tui is not enabled -- run `sukuna-cli init` "
            "and opt in to the tree TUI item first"
        )
    return tui_launcher.launch_tree_tui(
        tree_payload(
            registry,
            limit=None,
            since=since,
            to=to,
            cwd=cwd,
            resumable_only=resumable_only,
        ),
        state_dir_bundle_usable=state_dir_bundle_usable(),
    )

"""Render a `build_tree()` result for CLI display (presentation, not domain)."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_local_datetime(value: str) -> datetime:
    """Parse a naive local-time string into an aware datetime — the inverse
    of `format_local_timestamp`'s UTC-to-local conversion — so it can be
    compared against a stored aware-UTC `updated_at` value."""
    return datetime.fromisoformat(value).astimezone()


def format_local_timestamp(updated_at: str) -> str:
    """Render a stored UTC `updated_at` ISO string in the viewer's local timezone."""
    return datetime.fromisoformat(updated_at).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def render_compact_tree(
    tree: dict[str, list[Any]], *, hidden_parent_groups: int = 0
) -> str:
    """Render a `build_tree()` (optionally `select_parent_groups()`-selected)
    result as one box-drawn line per node.

    Each worker node prints as `${name} (${updated_at})`, with `updated_at`
    converted from the stored UTC value to the viewer's local timezone —
    goal/state are deliberately omitted; use `inspect` for those. A node
    carrying `resumable: True` (attached by
    `attach_resumable_flags()`, not this function) gets a trailing
    `[resumable]` tag; a node with no `resumable` key at all (nothing in
    this codebase produces that today) is treated the same as `False`. A
    cycle-detected child is already a plain string (from `build_tree`) and
    is printed as-is. `hidden_parent_groups`, when positive, appends a
    single trailing `"(+N more parent groups)"` line — parent groups are
    hidden as whole units by `select_parent_groups()`, never truncated
    member-by-member, so there is one marker for the whole output rather
    than one per group."""
    lines: list[str] = []
    for group_key, nodes in tree.items():
        lines.append(group_key)
        lines.extend(_render_nodes(nodes, ""))
        lines.append("")
    if hidden_parent_groups > 0:
        lines.append(f"(+{hidden_parent_groups} more parent groups)")
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _render_nodes(nodes: list[Any], prefix: str) -> list[str]:
    lines: list[str] = []
    last_index = len(nodes) - 1
    for index, node in enumerate(nodes):
        is_last = index == last_index
        connector = "└── " if is_last else "├── "
        continuation = "    " if is_last else "│   "
        if isinstance(node, str):
            lines.append(f"{prefix}{connector}{node}")
            continue
        tag = " [resumable]" if node.get("resumable") else ""
        lines.append(
            f"{prefix}{connector}{node['name']}"
            f" ({format_local_timestamp(node['updated_at'])}){tag}"
        )
        lines.extend(_render_nodes(node["children"], prefix + continuation))
    return lines

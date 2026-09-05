"""Build a nested worker tree from a flat list of records (pure function)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..entity.worker_record import WorkerRecord
from .resumability import is_resumable


def _on_cycle(name: str, by_name: dict[str, WorkerRecord]) -> bool:
    """True if walking `parent_worker_name` from `name` eventually returns
    to `name` itself -- i.e. `name` sits on a cycle, rather than merely
    hanging off one as a plain descendant. Stops (returns False) at a root
    (no `parent_worker_name`), an orphan (`parent_worker_name` not in
    `by_name`), or a different cycle that does not loop back through
    `name`."""
    seen: set[str] = set()
    current = name
    while True:
        worker = by_name.get(current)
        if worker is None or not worker.parent_worker_name:
            return False
        next_name = worker.parent_worker_name
        if next_name == name:
            return True
        if next_name in seen:
            return False
        seen.add(next_name)
        current = next_name


class _NodeBuilder:
    """Shared `visited` set + recursive node construction for `build_tree()`'s
    three passes (root/orphan/unreachable groups) -- each pass calls
    `build()` on the workers that belong to it, and all three need to see
    the same `visited` state so a worker already rendered by an earlier pass
    is never rendered again by a later one."""

    def __init__(self, children_by_parent_name: dict[str, list[WorkerRecord]]) -> None:
        self._children_by_parent_name = children_by_parent_name
        self.visited: set[str] = set()

    def build(self, worker: WorkerRecord, ancestors: frozenset[str]) -> dict[str, Any]:
        self.visited.add(worker.name)
        children = sorted(
            self._children_by_parent_name.get(worker.name, []),
            key=lambda child: datetime.fromisoformat(child.updated_at),
        )
        node_children: list[Any] = []
        for child in children:
            if child.name in ancestors:
                node_children.append(
                    f"(cycle detected: '{child.name}' already an ancestor)"
                )
                continue
            node_children.append(self.build(child, ancestors | {child.name}))
        return {
            "name": worker.name,
            "goal": worker.goal,
            "state": worker.state.value,
            "updated_at": worker.updated_at,
            "children": node_children,
        }


def _root_groups(
    by_parent: dict[str, list[WorkerRecord]], builder: _NodeBuilder
) -> dict[str, list[dict[str, Any]]]:
    """Workers with `parent_worker_name` unset, grouped by the Claude Code
    session that spawned them (`$CLAUDE_CODE_SESSION_ID` at spawn time)."""
    return {
        parent: [
            builder.build(child, frozenset({child.name}))
            for child in sorted(
                children, key=lambda child: datetime.fromisoformat(child.updated_at)
            )
        ]
        for parent, children in sorted(by_parent.items())
    }


def _orphan_groups(
    workers: list[WorkerRecord],
    by_name: dict[str, WorkerRecord],
    builder: _NodeBuilder,
    tree: dict[str, list[dict[str, Any]]],
) -> None:
    """A `parent_worker_name` that does not resolve to a registered worker
    (e.g. the parent was already closed) surfaces flat under an
    `"(orphaned: parent '<name>' not found)"` key, mutating `tree` in
    place."""
    for worker in sorted(
        workers, key=lambda worker: datetime.fromisoformat(worker.updated_at)
    ):
        if not worker.parent_worker_name or worker.parent_worker_name in by_name:
            continue
        key = f"(orphaned: parent '{worker.parent_worker_name}' not found)"
        tree.setdefault(key, []).append(builder.build(worker, frozenset({worker.name})))


def _unreachable_groups(
    workers: list[WorkerRecord],
    by_name: dict[str, WorkerRecord],
    builder: _NodeBuilder,
    tree: dict[str, list[dict[str, Any]]],
) -> None:
    """A worker can end up neither a root nor an orphan nor ever visited
    while building those two: an isolated `parent_worker_name` cycle (A ->
    B -> A, hand-edited into the registry) where every member points to
    another *existing* member. Surfaced once per connected component,
    under a `"(cycle: unreachable from any root)"` key: only workers that
    actually sit on the cycle (`_on_cycle()`) are candidates as top-level
    entries there, one per component, picking the `updated_at`-earliest
    cycle member as that component's representative. A plain descendant
    hanging off a cycle member is not such a candidate -- it is reached
    exactly once, through the representative's recursive `build()` call.
    `builder.visited` keeps every worker from appearing under this key a
    second time once already rendered elsewhere. Mutates `tree` in
    place."""
    unreachable_key = "(cycle: unreachable from any root)"
    for worker in sorted(
        workers, key=lambda worker: datetime.fromisoformat(worker.updated_at)
    ):
        if worker.name in builder.visited or not _on_cycle(worker.name, by_name):
            continue
        tree.setdefault(unreachable_key, []).append(
            builder.build(worker, frozenset({worker.name}))
        )

    # Defensive fallback: anything still unvisited at this point should be
    # empty in theory (every remaining worker's parent chain either reaches
    # a root/orphan, already visited above, or loops back through a cycle
    # member, just handled above) -- but surface it rather than drop it if
    # that invariant is ever wrong.
    for worker in sorted(
        workers, key=lambda worker: datetime.fromisoformat(worker.updated_at)
    ):
        if worker.name in builder.visited:
            continue
        tree.setdefault(unreachable_key, []).append(
            builder.build(worker, frozenset({worker.name}))
        )


def build_tree(workers: list[WorkerRecord]) -> dict[str, list[dict[str, Any]]]:
    """Build a tree of every registered worker. A worker whose
    `parent_worker_name` names a worker that exists nests under that
    worker's node, to arbitrary depth. See `_root_groups()`,
    `_orphan_groups()`, and `_unreachable_groups()` for the three groups a
    worker can end up in; this function builds the shared index
    (`children_by_parent_name`, `by_parent`) and merges the three groups'
    results."""
    by_name = {worker.name: worker for worker in workers}
    children_by_parent_name: dict[str, list[WorkerRecord]] = {}
    by_parent: dict[str, list[WorkerRecord]] = {}
    for worker in workers:
        if worker.parent_worker_name:
            children_by_parent_name.setdefault(worker.parent_worker_name, []).append(
                worker
            )
        else:
            key = worker.parent_session_id or "(unknown parent)"
            by_parent.setdefault(key, []).append(worker)

    builder = _NodeBuilder(children_by_parent_name)
    tree = _root_groups(by_parent, builder)
    _orphan_groups(workers, by_name, builder, tree)
    _unreachable_groups(workers, by_name, builder, tree)
    return tree


def attach_session_log_paths(
    tree: dict[str, list[Any]],
    worktree_by_name: dict[str, str],
    resolve: Callable[[str, str], str | None],
) -> None:
    """Mutate `tree` in place, adding a `session_log_path` key to every
    node dict -- `None` when `worktree_by_name` has no entry for the node
    or `resolve()` found no matching jsonl. `resolve` is injected rather
    than imported directly so this stays filesystem-free and
    unit-testable with a fake. `usecase/tree.py`'s `tree_payload()` -- the
    only real caller -- injects a pure `WorkerRecord.session_log_path`
    dict lookup here, not a filesystem probe: that value is resolved
    lazily at state-transition time by `infrastructure/registry.py`'s
    `mutate()`/`replace()` hook. The filesystem probe itself lives in
    `infrastructure/claude_projects.py`, used by that hook and by
    `resolve_group_session_log_paths()` below. Cycle-detected children
    (plain strings, not dicts) are left as-is."""

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if isinstance(node, str):
                continue
            worktree = worktree_by_name.get(node["name"])
            node["session_log_path"] = (
                resolve(worktree, node["name"]) if worktree is not None else None
            )
            walk(node["children"])

    for nodes in tree.values():
        walk(nodes)


def attach_resumable_flags(
    tree: dict[str, list[Any]], by_name: dict[str, WorkerRecord], cwd: str
) -> None:
    """Mutate `tree` in place, adding a `resumable` key (see
    `domain.service.resumability.is_resumable()`) to every real worker
    node. Group keys (`parent_session_id`/`"(unknown parent)"`/
    `"(orphaned: ...)"`/`"(cycle: ...)"`) are plain strings, not nodes,
    and are left untouched; a cycle-detected child is skipped the same
    way. `by_name` is injected rather than re-derived from `tree` because
    a node dict carries no `worktree` field."""

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if isinstance(node, str):
                continue
            worker = by_name.get(node["name"])
            node["resumable"] = worker is not None and is_resumable(worker, cwd)
            walk(node["children"])

    for nodes in tree.values():
        walk(nodes)


def prune_to_resumable(tree: dict[str, list[Any]]) -> dict[str, list[Any]]:
    """Bottom-up filter of an `attach_resumable_flags()`-annotated tree: a
    node survives if it is itself resumable or at least one descendant is
    -- a non-resumable ancestor with a resumable descendant survives too,
    `resumable: False`, as context for reaching it. A cycle-detected child
    carries no resumability information of its own and is dropped. A
    top-level group key is dropped only once every one of its members is
    pruned away. Returns a new tree rather than mutating in place,
    mirroring `select_parent_groups()`."""

    def prune_nodes(nodes: list[Any]) -> list[Any]:
        survivors: list[Any] = []
        for node in nodes:
            if isinstance(node, str):
                continue
            pruned_children = prune_nodes(node["children"])
            if node["resumable"] or pruned_children:
                survivors.append({**node, "children": pruned_children})
        return survivors

    pruned = {group_key: prune_nodes(nodes) for group_key, nodes in tree.items()}
    return {group_key: nodes for group_key, nodes in pruned.items() if nodes}


def resolve_group_session_log_paths(
    tree: dict[str, list[Any]], resolve: Callable[[str], str | None]
) -> dict[str, str | None]:
    """Resolve a `session_log_path` for each top-level group key of a
    `build_tree()` result -- a selected parent row's own session history,
    distinct from `attach_session_log_paths()`'s per-node resolution.
    Returns a new dict rather than mutating `tree` in place: group keys
    are plain strings, not dicts. `resolve` is injected the same way
    `attach_session_log_paths()` injects its resolver; the real probe
    lives in `infrastructure/claude_projects.py`.

    A group key that is not a real `parent_session_id` (`"(unknown
    parent)"`, `"(orphaned: ...)"`, or `"(cycle: ...)"`, the only three
    sentinels `build_tree()` ever emits) maps straight to `None` without
    calling `resolve()`: all three happen to start with `"("`, which a
    real session UUID never does, so that prefix check tells sentinel and
    real key apart without hardcoding the three literal strings."""
    return {key: (None if key.startswith("(") else resolve(key)) for key in tree}


def find_deepest_busy(
    tree: dict[str, list[dict[str, Any]]], root_key: str
) -> str | None:
    """Find the name of the deepest BUSY worker under `root_key` (a
    `build_tree()` group key). Depth is the primary sort key, `updated_at`
    only breaks ties at equal depth. `updated_at` is parsed via
    `datetime.fromisoformat()` for comparison, the same way `build_tree()`
    sorts -- a raw string comparison only agrees with time order when
    every value shares the same UTC offset. Cycle-detected children come
    back as plain strings, never dicts -- skipped via `isinstance`."""
    best: tuple[int, datetime, str] | None = None

    def walk(nodes: list[Any], depth: int) -> None:
        nonlocal best
        for node in nodes:
            if isinstance(node, str):
                continue
            if node["state"] == "busy":
                candidate = (
                    depth,
                    datetime.fromisoformat(node["updated_at"]),
                    node["name"],
                )
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
            walk(node["children"], depth + 1)

    walk(tree.get(root_key, []), 0)
    return best[2] if best else None


def select_parent_groups(
    tree: dict[str, list[dict[str, Any]]],
    *,
    limit: int | None,
    since: datetime | None = None,
    to: datetime | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Select which root-level parent groups of a `build_tree()` result to
    show, and in what order. Operates on whole parent groups, never on
    what is inside one: a selected group's nodes are returned exactly as
    `build_tree()` produced them.

    Each parent group's representative timestamp is its most recently
    updated direct member (`build_tree()` already sorts each group's node
    list ascending by `updated_at`). A group whose representative
    timestamp falls outside the inclusive `[since, to]` range is dropped.
    Surviving groups are ordered by representative timestamp descending
    and truncated to `limit`: `None` keeps every group, `0` hides every
    group, `>0` keeps the top `limit`.

    Returns the selected groups (in display order) plus the count of
    parent groups the range/limit selection left out, for the caller to
    surface as a single trailing marker rather than one per group."""
    candidates = [
        (group_key, nodes, datetime.fromisoformat(nodes[-1]["updated_at"]))
        for group_key, nodes in tree.items()
    ]
    in_range = [
        (group_key, nodes, when)
        for group_key, nodes, when in candidates
        if (since is None or when >= since) and (to is None or when <= to)
    ]
    in_range.sort(key=lambda candidate: candidate[2], reverse=True)
    visible = in_range if limit is None else (in_range[:limit] if limit > 0 else [])
    hidden = len(in_range) - len(visible)
    return {group_key: nodes for group_key, nodes, _ in visible}, hidden

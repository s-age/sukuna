"""Helpers shared between `usecase/spawn.py`, `usecase/respawn.py`, and
`usecase/close.py`: placement resolution, the cross-backend continuation
guard, best-effort failure compensation, and the two post-spawn
best-effort follow-ups (pending width convergence, column
equalization)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NoReturn

from ..domain.entity.pane import SplitDirection
from ..domain.entity.worker_record import WorkerRecord
from ..domain.mapper.terminal_mapper import SpawnResult
from ..domain.service.spawn_placement import (
    alive_pane_holders,
    choose_anchor,
    column_of,
    equalize_target,
    existing_workers,
)
from ..errors import BackendError, CrossBufferError, ValidationError
from ..infrastructure.terminal import operations as terminal_ops
from ..infrastructure.terminal.operations import SpawnOutcome
from ..infrastructure.terminal.resolver import infer_backend
from .pane_convergence import converge_active_pane_width
from .window_frame import preserve_window_frame


def _resolve_anchor(
    children: list[WorkerRecord], parent_pane_ref: str | None
) -> tuple[str, SplitDirection, list[str] | None] | None:
    anchor = choose_anchor(children, parent_pane_ref)
    if anchor is None:
        return None
    return (*anchor, equalize_target(children))


def _reject_placement_backend_mismatch(
    *,
    name: str | None,
    anchor_pane_ref: str,
    orchestrator_ref: str | None,
    backend: str,
) -> None:
    """`pane_holders` is registry-wide (no session/backend scope, unlike
    `root_children()`) -- a `parent_worker_name` can resolve to a worker
    from a different session/backend entirely. This is rejected here,
    right where the anchor is decided."""
    if not (name or anchor_pane_ref != orchestrator_ref):
        return
    found_backend = infer_backend(anchor_pane_ref)
    if found_backend == backend:
        return
    who = (
        f"parent worker {name!r}'s" if name else "the resolved lineage's last surviving"
    )
    raise ValidationError(
        f"{who} pane looks like {found_backend!r}, but "
        f"the current environment resolved to {backend!r}; refusing to mix backends"
    )


def _placement_for(
    name: str | None,
    *,
    pane_holders: list[WorkerRecord],
    orchestrator_ref: str | None,
    parent_session_id: str | None,
    backend: str,
) -> tuple[str, SplitDirection, list[str] | None] | None:
    parent_pane_ref = orchestrator_ref
    if name:
        parent = next((w for w in pane_holders if w.name == name), None)
        if parent is None:
            return None  # parent name unresolvable (typo, already closed, or
            # pane-less FAILED -- all three are already dropped from
            # pane_holders by alive_pane_holders() at this point, so
            # they're all handled by this same branch)
        parent_pane_ref = parent.pane_ref
    children = column_of(
        pane_holders, parent_worker_name=name, parent_session_id=parent_session_id
    )
    resolved = _resolve_anchor(children, parent_pane_ref)
    if resolved is None:
        return None
    _reject_placement_backend_mismatch(
        name=name,
        anchor_pane_ref=resolved[0],
        orchestrator_ref=orchestrator_ref,
        backend=backend,
    )
    return resolved


def _initial_placement(
    parent_worker_name: str | None,
    *,
    placement_for: Callable[
        [str | None], tuple[str, SplitDirection, list[str] | None] | None
    ],
) -> tuple[tuple[str, SplitDirection, list[str] | None], bool]:
    used_root_from_start = not parent_worker_name
    placement = placement_for(parent_worker_name if parent_worker_name else None)
    if placement is None and not used_root_from_start:
        used_root_from_start = True  # parent name unresolvable -> switch to root, treat as root from here on
        placement = placement_for(None)
    if placement is None:
        raise ValidationError("no anchor pane available to spawn from")
    return placement, used_root_from_start


def _verify_placement_with_fallback(
    placement: tuple[str, SplitDirection, list[str] | None],
    *,
    used_root_from_start: bool,
    backend: str,
    placement_for: Callable[
        [str | None], tuple[str, SplitDirection, list[str] | None] | None
    ],
) -> tuple[str, SplitDirection, list[str] | None]:
    anchor, direction, eq_target = placement
    if terminal_ops.verify(backend=backend, pane_ref=anchor).exists:
        return anchor, direction, eq_target

    if not used_root_from_start:
        # Falls back to root exactly once, only when a child-scope anchor
        # has disappeared. When already root-derived (used_root_from_start
        # is True), there is no further fallback target, so this does not
        # recompute -- avoids the waste of verifying the same anchor twice.
        fallback = placement_for(None)
        if fallback is not None:
            anchor, direction, eq_target = fallback
            if terminal_ops.verify(backend=backend, pane_ref=anchor).exists:
                return anchor, direction, eq_target

    raise ValidationError(
        f"anchor pane {anchor!r} no longer exists and no fallback anchor is available"
    )


def resolve_placement(
    records: list[WorkerRecord],
    *,
    backend: str,
    parent_session_id: str | None,
    parent_worker_name: str | None,
    orchestrator_ref: str | None,
) -> tuple[str, SplitDirection, list[str] | None]:
    pane_holders = alive_pane_holders(records)

    def placement_for(
        name: str | None,
    ) -> tuple[str, SplitDirection, list[str] | None] | None:
        return _placement_for(
            name,
            pane_holders=pane_holders,
            orchestrator_ref=orchestrator_ref,
            parent_session_id=parent_session_id,
            backend=backend,
        )

    placement, used_root_from_start = _initial_placement(
        parent_worker_name, placement_for=placement_for
    )
    return _verify_placement_with_fallback(
        placement,
        used_root_from_start=used_root_from_start,
        backend=backend,
        placement_for=placement_for,
    )


def reraise_after_best_effort(
    original: CrossBufferError, action: str, write: Callable[[], object]
) -> NoReturn:
    """Runs a best-effort registry write triggered by `original`'s failure,
    then always re-raises `original` -- the write's own `CrossBufferError`
    must never override the `error.code` already being reported. If the
    write itself fails, its message is folded into `original`'s instead of
    being silently dropped."""
    try:
        write()
    except CrossBufferError as write_error:
        raise type(original)(
            f"{original} (also failed to {action}: {write_error})"
        ) from original
    raise original


def apply_pending_active_pane_width(
    *, backend: str, window_ref: str | None, pane_ref: str, percent: int
) -> str | None:
    """Applies `percent` as a separate resize() retry loop, saving/restoring
    the window's frame. Best-effort: neither a convergence nor a frame
    save/restore failure may fail an already-spawned, live worker -- a
    convergence failure degrades to the returned warning string; a frame
    failure is silently swallowed."""
    with preserve_window_frame(backend=backend, window_ref=window_ref):
        try:
            response, convergence_warning = converge_active_pane_width(
                backend=backend, pane_ref=pane_ref, percent=percent
            )
        except BackendError as error:
            return str(error)
    return (
        convergence_warning
        if convergence_warning is not None
        else response.resize_warning
    )


def reject_cross_backend_continuation(
    records: list[WorkerRecord], *, parent_session_id: str | None, backend: str
) -> None:
    """Must run before `registry.add()`/`registry.transition(..., STARTING)`
    -- a rejected mismatch leaves no trace in the registry.
    `parent_session_id` is always the *current* orchestrator session, never
    a respawn target's stored value -- this checks today's pane tree, not
    the record being (re)spawned."""
    layout = alive_pane_holders(existing_workers(records, parent_session_id))
    if not layout:
        return
    last_pane_ref = layout[-1].pane_ref
    assert last_pane_ref is not None
    if infer_backend(last_pane_ref) != backend:
        raise ValidationError(
            f"the last worker's pane looks like {infer_backend(last_pane_ref)!r}, "
            f"but the current environment resolved to {backend!r}; refusing to mix backends"
        )


def equalize_new_pane(
    *, backend: str, eq_target: list[str] | None, pane_ref: str
) -> str | None:
    if eq_target is None:
        return None
    try:
        terminal_ops.equalize(backend=backend, column_pane_refs=[*eq_target, pane_ref])
    except BackendError as error:
        return str(error)
    return None


def finalize_spawned_pane(
    *, backend: str, spawned: SpawnOutcome, eq_target: list[str] | None
) -> tuple[SpawnResult, str | None, str | None]:
    """The one post-spawn follow-up sequence both `spawn.py::_spawn_one` and
    `respawn.py::respawn` must run after a successful pane split: always
    defer-apply the pending active_pane_width, then equalize the column,
    downgrading either failure to a warning instead of failing a live
    worker. Returns `(validated, resize_warning, equalize_warning)`."""
    validated = spawned.result
    resize_warning = validated.resize_warning
    if spawned.pending_active_pane_width is not None:
        resize_warning = apply_pending_active_pane_width(
            backend=backend,
            window_ref=validated.window_ref,
            pane_ref=validated.pane_ref,
            percent=spawned.pending_active_pane_width,
        )

    equalize_warning = equalize_new_pane(
        backend=backend, eq_target=eq_target, pane_ref=validated.pane_ref
    )
    return validated, resize_warning, equalize_warning


def attach_spawn_warnings(
    result: dict[str, Any], *, equalize_warning: str | None, resize_warning: str | None
) -> dict[str, Any]:
    """Attach the two best-effort follow-up warnings to a spawn/respawn
    result dict, each only when present."""
    if equalize_warning is not None:
        result["equalize_warning"] = equalize_warning
    if resize_warning is not None:
        result["resize_warning"] = resize_warning
    return result

"""Resize a pane to the configured `active_pane_width` (from the
settings file, default 50; never a CLI argument) -- either the
orchestrator's own pane (no `--worker`) or a named managed worker's
pane. The self path only runs for a session recognized as sukuna-related
(spawn history of its own, or its own pane matching a live sukuna worker
record), and only actually moves anything when the asking pane is not
already the terminal's active pane."""

from __future__ import annotations

import os
from typing import Any

from ..domain.mapper.command_mapper import validate_name
from ..domain.service.spawn_placement import matches_alive_pane_ref
from ..errors import BackendError, ValidationError
from ..infrastructure.registry import Registry
from ..infrastructure.settings import load_active_pane_width, load_should_focus_worker
from ..infrastructure.terminal import operations as terminal_ops
from ..infrastructure.terminal.resolver import (
    detect_backend,
    infer_backend,
    orchestrator_pane_ref,
)
from .pane_convergence import converge_active_pane_width
from .window_frame import preserve_window_frame

_NOT_SUKUNA_RELATED_SKIP = (
    "this session is not recognized as sukuna-related (never spawned a "
    "worker, and its own pane is not a live sukuna worker record)"
)
_ALREADY_ACTIVE_SKIP = "the asking pane is already the active pane"


def _has_ever_spawned(registry: Registry, session_id: str) -> bool:
    """Reads `Registry.list()` raw, including CLOSED records: `True` once
    a session has spawned anything, even after every child has since
    been closed."""
    return any(record.parent_session_id == session_id for record in registry.list())


def _is_sukuna_related(
    *,
    ever_spawned: bool,
    pane_registry: Registry,
    own_pane_ref: str | None,
) -> bool:
    """Gate 1 (sukuna-relatedness): `ever_spawned` is the caller's own,
    already-computed `_has_ever_spawned()` result -- taken as a parameter
    rather than recomputed here, to avoid doubling the `registry.list()`
    read on the never-spawned fallback path, which has already computed
    the same result once. Only a session with no spawn history of its own
    falls through to check whether its own pane belongs to a live sukuna
    worker record instead -- a worker session, whose own
    $CLAUDE_CODE_SESSION_ID sukuna never observes (CLAUDE.md: on its own,
    parent_session_id does not connect more than one level deep)."""
    if ever_spawned:
        return True
    if own_pane_ref is None:
        return False
    return matches_alive_pane_ref(pane_registry.list(), own_pane_ref)


def _resolve_own_pane_or_none() -> tuple[str, str] | None:
    """Best-effort `(pane_ref, backend)` resolution for the never-spawned
    self path's gate-2 check: `None` on any failure, including a bare
    environment (`detect_backend()` itself raising `ValidationError`) --
    fail-closed, so an unresolvable own pane just means gate 2 can't
    match, the same as a routine skip, never a propagated error."""
    try:
        backend = detect_backend()
    except ValidationError:
        return None
    pane_ref = orchestrator_pane_ref(backend)
    if pane_ref is None:
        return None
    return pane_ref, backend


def _resolve_self_target(
    registry: Registry, pane_registry: Registry
) -> tuple[str, str, str | None] | None:
    """The `name is None` half of `_resolve_target()`, split out to keep
    both functions under the branch-count limit."""
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    # Computed once and threaded through (rather than let `_is_sukuna_
    # related()` recompute it) -- see that function's docstring, finding 1.
    ever_spawned = session_id is not None and _has_ever_spawned(registry, session_id)
    if ever_spawned:
        # Ordered before the never-spawned branch's fail-closed
        # resolution on purpose: with neither $TMUX nor $ITERM_SESSION_ID
        # set, `detect_backend()` itself raises `ValidationError` -- for
        # an already-spawned session that is a real error (below), not a
        # routine skip.
        backend = detect_backend()
        pane_ref = orchestrator_pane_ref(backend)
        if pane_ref is None:
            raise ValidationError(
                "could not resolve the orchestrator's own pane from the environment"
            )
        return pane_ref, backend, None

    resolved = _resolve_own_pane_or_none()
    own_pane_ref = resolved[0] if resolved is not None else None
    if not _is_sukuna_related(
        ever_spawned=ever_spawned,
        pane_registry=pane_registry,
        own_pane_ref=own_pane_ref,
    ):
        return None
    # `resolved is None` implies `own_pane_ref is None`, which the guard
    # above already forces `_is_sukuna_related()` to reject -- so, with
    # `ever_spawned` now read exactly once (finding 1's fix), this branch
    # is unreachable in practice. Kept anyway: (1) it is what lets mypy
    # narrow `resolved` from `tuple[str, str] | None` before the unpack
    # below, and (2) before this round's fix, it was genuinely
    # load-bearing -- a concurrent spawn from this same session landing
    # between two separate `_has_ever_spawned()` reads could make the
    # second read see `True` while the pre-race `resolved` was still
    # `None`. That specific race is closed now, but the guard costs
    # nothing to keep and protects against a future reintroduction of a
    # second read.
    if resolved is None:
        return None
    pane_ref, backend = resolved
    # No `WorkerRecord` exists for the orchestrator's own pane, so there is
    # no registered `window_ref` to key frame save/restore off of --
    # resolved live from `pane_ref` in `resize()` instead: the pane is
    # still alive here, unlike `close()`'s already-destroyed pane, so there
    # is no need to have pre-recorded it.
    return pane_ref, backend, None


def _resolve_target(
    registry: Registry, name: str | None, pane_registry: Registry
) -> tuple[str, str, str | None] | None:
    """Resolves the self path (`name is None`) or the named-worker path to
    `(pane_ref, backend, window_ref)`. Returns `None` for the self path's
    sukuna-relatedness gate instead of raising -- that path is a routine
    skip, not an error."""
    if name is None:
        return _resolve_self_target(registry, pane_registry)

    name = validate_name(name)
    worker = registry.get(name)
    if worker.pane_ref is None:
        raise ValidationError(f"worker {name!r} has no pane to resize")
    return worker.pane_ref, infer_backend(worker.pane_ref), worker.window_ref


def _self_path_is_active(backend: str, pane_ref: str) -> tuple[bool, str | None]:
    """Gate 2 (self path only): whether the asking pane is already the
    terminal's active one. A `BackendError` fails open (treated as "not
    active", so the usual widen+select_pane still runs), returned as a
    warning instead of failing the whole call -- same posture as
    resize_warning/select_pane_warning/convergence_warning."""
    try:
        return terminal_ops.is_active(backend=backend, pane_ref=pane_ref).active, None
    except BackendError as error:
        return False, str(error)


def _resolve_window_ref_or_none(backend: str, pane_ref: str) -> str | None:
    # `resolve_window_ref()` is itself a no-op off iTerm2 (see
    # `infrastructure.terminal.operations.resolve_window_ref()`) -- no need
    # to gate the call on `backend` here too.
    try:
        return terminal_ops.resolve_window_ref(backend=backend, pane_ref=pane_ref)
    except BackendError:
        return None


def _apply_resize(
    *, backend: str, pane_ref: str, window_ref: str | None
) -> dict[str, Any]:
    """Runs the window-frame-preserving convergence loop and returns its
    result dict, with `resize_warning` merged in when the loop never
    reached tolerance."""
    if window_ref is None:
        window_ref = _resolve_window_ref_or_none(backend, pane_ref)
    percent = load_active_pane_width()
    # Best-effort, same posture as spawn's retry-loop wrapper.
    with preserve_window_frame(backend=backend, window_ref=window_ref):
        response, convergence_warning = converge_active_pane_width(
            backend=backend, pane_ref=pane_ref, percent=percent
        )
    result = response.model_dump(exclude_none=True)
    # The loop's own could-not-converge warning takes priority over a stale
    # per-attempt `resize_warning` (e.g. a safety clamp) from an
    # intermediate, non-final requested percent.
    if convergence_warning is not None:
        result["resize_warning"] = convergence_warning
    return result


def _apply_select_pane(result: dict[str, Any], *, backend: str, pane_ref: str) -> None:
    try:
        terminal_ops.select_pane(backend=backend, pane_ref=pane_ref)
    except BackendError as error:
        result["select_pane_warning"] = str(error)


def resize(
    registry: Registry, name: str | None, *, pane_registry: Registry | None = None
) -> dict[str, Any]:
    """`pane_registry` backs gate 2's cross-shard pane_ref lookup -- callers
    with no shard-aware registry (tests, `--registry` overrides) may omit
    it, in which case it defaults to `registry` itself."""
    pane_registry = pane_registry if pane_registry is not None else registry
    target = _resolve_target(registry, name, pane_registry)
    if target is None:
        return {"ok": True, "skipped": _NOT_SUKUNA_RELATED_SKIP}
    pane_ref, backend, window_ref = target

    is_active_warning: str | None = None
    if name is None:
        active, is_active_warning = _self_path_is_active(backend, pane_ref)
        if active:
            return {"ok": True, "skipped": _ALREADY_ACTIVE_SKIP}

    result = _apply_resize(backend=backend, pane_ref=pane_ref, window_ref=window_ref)
    if is_active_warning is not None:
        result["is_active_warning"] = is_active_warning

    if name is None or load_should_focus_worker():
        _apply_select_pane(result, backend=backend, pane_ref=pane_ref)
    return result

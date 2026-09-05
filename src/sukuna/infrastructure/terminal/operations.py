"""Typed operations facade over the raw terminal backend wire protocol.
Consolidates the dict-build -> BACKENDS dispatch -> run() -> parse_backend_response
pattern used by the usecase layer's spawn/capture/layout/reconcile/close
flows.
"""

from __future__ import annotations

from typing import NamedTuple

from ...domain.entity.pane import SplitDirection
from ...domain.mapper.terminal_mapper import (
    CaptureRequest,
    CaptureResponse,
    CloseRequest,
    CloseResponse,
    EqualizeRequest,
    EqualizeResponse,
    GetWindowFrameRequest,
    IsActiveRequest,
    IsActiveResponse,
    PaneHeightsRequest,
    PaneHeightsResponse,
    ResizeContextRequest,
    ResizeContextResponse,
    ResizeRequest,
    ResizeResponse,
    ResolveWindowRefRequest,
    ResolveWindowRefResponse,
    SelectPaneRequest,
    SelectPaneResponse,
    SetPaneHeightRequest,
    SetPaneHeightResponse,
    SetWindowFrameRequest,
    SetWindowFrameResponse,
    SpawnRequest,
    SpawnResult,
    VerifyRequest,
    VerifyResponse,
    WindowFrameResponse,
    parse_backend_response,
)
from ...domain.service.pane_height import equalize_heights
from ...domain.service.pane_width import compute_clamped_percent
from . import BACKENDS


class SpawnOutcome(NamedTuple):
    """`operations.spawn()`'s own return shape -- distinct from `SpawnResult`,
    which stays a pure representation of a backend's raw response: the two
    must not be conflated. `pending_active_pane_width` is computed by
    `spawn()` itself, never by a backend, so it has no place on
    `SpawnResult`."""

    result: SpawnResult
    pending_active_pane_width: int | None = None


def _tmux_resize_with_clamp(*, pane_ref: str, percent: int) -> ResizeResponse:
    """Assembles tmux's `resize` around the clamping policy: queries
    `resize_context`, computes the clamp, then issues the raw
    `resize`."""
    context_raw = BACKENDS["tmux"]().run(
        ResizeContextRequest(operation="resize_context", pane_ref=pane_ref).model_dump()
    )
    context = parse_backend_response(ResizeContextResponse, context_raw)
    clamped_percent, warning = compute_clamped_percent(
        window_width=context.window_width,
        other_pane_count=context.other_pane_count,
        percent=percent,
    )
    raw = BACKENDS["tmux"]().run(
        ResizeRequest(
            operation="resize", pane_ref=pane_ref, percent=clamped_percent
        ).model_dump()
    )
    response = parse_backend_response(ResizeResponse, raw)
    if warning is not None:
        response = response.model_copy(update={"resize_warning": warning})
    return response


def spawn(
    *,
    backend: str,
    command: str,
    anchor_pane_ref: str | None,
    split_direction: SplitDirection,
    active_pane_width: int | None = None,
) -> SpawnOutcome:
    """Every backend spawn is a plain split: neither backend applies
    `active_pane_width` inline here. It is always handed back via
    `SpawnOutcome.pending_active_pane_width` for the caller
    (`usecase.spawn_shared.apply_pending_active_pane_width`) to apply as
    its own follow-up `resize()` call, with a "warn, don't fail" posture --
    a resize failure there must not turn a live, running worker into a
    FAILED one with no pane_ref for `reconcile` to repair."""
    request = SpawnRequest(
        operation="spawn",
        command=command,
        anchor_pane_ref=anchor_pane_ref,
        split_direction=split_direction,
    ).model_dump()
    raw = BACKENDS[backend]().run(request)
    result = parse_backend_response(SpawnResult, raw)
    return SpawnOutcome(result=result, pending_active_pane_width=active_pane_width)


def resize(*, backend: str, pane_ref: str, percent: int) -> ResizeResponse:
    if backend == "tmux":
        return _tmux_resize_with_clamp(pane_ref=pane_ref, percent=percent)
    request = ResizeRequest(
        operation="resize", pane_ref=pane_ref, percent=percent
    ).model_dump()
    raw = BACKENDS[backend]().run(request)
    return parse_backend_response(ResizeResponse, raw)


def verify(*, backend: str, pane_ref: str) -> VerifyResponse:
    request = VerifyRequest(operation="verify", pane_ref=pane_ref).model_dump()
    raw = BACKENDS[backend]().run(request)
    return parse_backend_response(VerifyResponse, raw)


def select_pane(*, backend: str, pane_ref: str) -> None:
    request = SelectPaneRequest(operation="select_pane", pane_ref=pane_ref).model_dump()
    raw = BACKENDS[backend]().run(request)
    parse_backend_response(SelectPaneResponse, raw)


def is_active(*, backend: str, pane_ref: str) -> IsActiveResponse:
    request = IsActiveRequest(operation="is_active", pane_ref=pane_ref).model_dump()
    raw = BACKENDS[backend]().run(request)
    return parse_backend_response(IsActiveResponse, raw)


def capture(*, backend: str, pane_ref: str) -> CaptureResponse:
    request = CaptureRequest(operation="capture", pane_ref=pane_ref).model_dump()
    raw = BACKENDS[backend]().run(request)
    return parse_backend_response(CaptureResponse, raw)


def close(*, backend: str, pane_ref: str) -> CloseResponse:
    request = CloseRequest(operation="close", pane_ref=pane_ref).model_dump()
    raw = BACKENDS[backend]().run(request)
    return parse_backend_response(CloseResponse, raw)


def _tmux_equalize(*, column_pane_refs: list[str]) -> EqualizeResponse:
    """Assembles tmux's `equalize` around the target-height policy: reads
    `pane_heights` (also checks every column member exists), computes
    the target via `domain.service.pane_height.equalize_heights`, then
    writes `set_pane_height` to the first N-1 members (the last one
    auto-absorbs)."""
    heights_raw = BACKENDS["tmux"]().run(
        PaneHeightsRequest(
            operation="pane_heights", column_pane_refs=column_pane_refs
        ).model_dump()
    )
    heights = parse_backend_response(PaneHeightsResponse, heights_raw).heights
    target = equalize_heights(heights)
    for pane_ref in column_pane_refs[:-1]:
        raw = BACKENDS["tmux"]().run(
            SetPaneHeightRequest(
                operation="set_pane_height", pane_ref=pane_ref, height=target
            ).model_dump()
        )
        parse_backend_response(SetPaneHeightResponse, raw)
    return EqualizeResponse(ok=True)


def equalize(*, backend: str, column_pane_refs: list[str]) -> EqualizeResponse:
    if backend == "tmux":
        return _tmux_equalize(column_pane_refs=column_pane_refs)
    request = EqualizeRequest(
        operation="equalize", column_pane_refs=column_pane_refs
    ).model_dump()
    raw = BACKENDS[backend]().run(request)
    return parse_backend_response(EqualizeResponse, raw)


def get_window_frame(
    *, backend: str, window_ref: str | None
) -> WindowFrameResponse | None:
    """Keyed by `window_ref` (not `pane_ref`): `App.get_window_by_id()`
    only needs the window to still exist, not the specific pane that
    first resolved it. Returns `None` when `backend != "iterm2"` or
    `window_ref` is `None`."""
    if backend != "iterm2" or window_ref is None:
        return None
    request = GetWindowFrameRequest(
        operation="get_window_frame", window_ref=window_ref
    ).model_dump()
    raw = BACKENDS[backend]().run(request)
    return parse_backend_response(WindowFrameResponse, raw)


def set_window_frame(
    *, backend: str, window_ref: str | None, frame: WindowFrameResponse
) -> None:
    if backend != "iterm2" or window_ref is None:
        return
    request = SetWindowFrameRequest(
        operation="set_window_frame",
        window_ref=window_ref,
        x=frame.x,
        y=frame.y,
        width=frame.width,
        height=frame.height,
    ).model_dump()
    raw = BACKENDS[backend]().run(request)
    parse_backend_response(SetWindowFrameResponse, raw)


def resolve_window_ref(*, backend: str, pane_ref: str) -> str | None:
    """Live counterpart to a `WorkerRecord.window_ref` lookup, for callers
    (self-resize) with no registered record to key off of: the pane is still
    alive at call time (unlike `close()`'s already-destroyed pane), so its
    containing window can be resolved through the pane itself instead."""
    if backend != "iterm2":
        return None
    request = ResolveWindowRefRequest(
        operation="resolve_window_ref", pane_ref=pane_ref
    ).model_dump()
    raw = BACKENDS[backend]().run(request)
    return parse_backend_response(ResolveWindowRefResponse, raw).window_ref

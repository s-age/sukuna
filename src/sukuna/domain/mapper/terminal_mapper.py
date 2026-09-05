"""pydantic models for translating terminal backend result dicts, and the
pure dict-derived-value -> entity conversion helpers built on top of them.
"""

from __future__ import annotations

from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from ...errors import BackendError
from ..entity.pane import SplitDirection

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def parse_backend_response(model: type[_ModelT], raw: dict[str, Any]) -> _ModelT:
    """The sole entry point converting a terminal/backend raw dict into a
    pydantic model. Translates a validation failure into `BackendError`
    (a `CrossBufferError` subclass) -- the CLI's error vocabulary does not
    know a raw pydantic `ValidationError`, so without this translation it
    would exit with a traceback."""
    try:
        return model.model_validate(raw)
    except PydanticValidationError as error:
        raise BackendError(
            f"backend returned an invalid {model.__name__} response: {error}"
        ) from error


class VerifyRequest(BaseModel):
    operation: Literal["verify"]
    pane_ref: str


class VerifyResponse(BaseModel):
    ok: Literal[True]
    exists: bool


class SelectPaneRequest(BaseModel):
    operation: Literal["select_pane"]
    pane_ref: str


class SelectPaneResponse(BaseModel):
    ok: Literal[True]


class IsActiveRequest(BaseModel):
    operation: Literal["is_active"]
    pane_ref: str


class IsActiveResponse(BaseModel):
    ok: Literal[True]
    active: bool


class SpawnRequest(BaseModel):
    operation: Literal["spawn"]
    command: str
    anchor_pane_ref: str | None
    split_direction: SplitDirection


class SpawnResult(BaseModel):
    ok: Literal[True]
    pane_ref: str = Field(min_length=1)
    window_ref: str | None = Field(default=None, min_length=1)
    resize_warning: str | None = None


class ResizeRequest(BaseModel):
    operation: Literal["resize"]
    pane_ref: str
    percent: int


class ResizeContextRequest(BaseModel):
    operation: Literal["resize_context"]
    pane_ref: str


class ResizeContextResponse(BaseModel):
    """Raw tmux state a resize needs to clamp against -- `tmux_backend.py`
    only resolves and reports this; `domain.service.pane_width.compute_
    clamped_percent()` is the only place that turns it into a clamping
    decision."""

    ok: Literal[True]
    window_width: int = Field(gt=0)
    other_pane_count: int = Field(ge=0)


class ResizeResponse(BaseModel):
    ok: Literal[True]
    resize_warning: str | None = None
    # The actual post-`async_update_layout()` occupancy, read back via a
    # fresh `async_get_app()` rather than the just-written app/session
    # objects, which stay stale. `None` when the backend has nothing to
    # report (e.g. the sibling lookup failed).
    achieved_percent: int | None = None


class CloseRequest(BaseModel):
    operation: Literal["close"]
    pane_ref: str


class CloseResponse(BaseModel):
    ok: Literal[True]
    # iTerm2 only (Q2/Q10-shaped drift): `async_close()`'s RPC success does
    # not guarantee the internal layout tree has caught up to the pane's
    # removal yet. `None` when the backend confirmed removal (or has no such
    # drift to report, e.g. tmux's synchronous `kill-pane`).
    close_warning: str | None = None


class GetWindowFrameRequest(BaseModel):
    operation: Literal["get_window_frame"]
    window_ref: str


class SetWindowFrameRequest(BaseModel):
    operation: Literal["set_window_frame"]
    window_ref: str
    x: float
    y: float
    width: float
    height: float


class SetWindowFrameResponse(BaseModel):
    ok: Literal[True]


class WindowFrameResponse(BaseModel):
    ok: Literal[True]
    x: float
    y: float
    width: float
    height: float


class ResolveWindowRefRequest(BaseModel):
    operation: Literal["resolve_window_ref"]
    pane_ref: str


class ResolveWindowRefResponse(BaseModel):
    ok: Literal[True]
    window_ref: str | None = None


class EqualizeRequest(BaseModel):
    operation: Literal["equalize"]
    column_pane_refs: list[str] = Field(min_length=2)


class EqualizeResponse(BaseModel):
    ok: Literal[True]


class PaneHeightsRequest(BaseModel):
    """tmux only: the raw heights read, half of a two-part wire op.
    Target-height arithmetic lives in
    `domain.service.pane_height.equalize_heights`, assembled with
    per-pane `set_pane_height` writes by `infrastructure.terminal.
    operations.equalize()` -- iTerm2 uses a single `equalize` op
    instead."""

    operation: Literal["pane_heights"]
    column_pane_refs: list[str] = Field(min_length=2)


class PaneHeightsResponse(BaseModel):
    ok: Literal[True]
    heights: list[int] = Field(min_length=2)


class SetPaneHeightRequest(BaseModel):
    operation: Literal["set_pane_height"]
    pane_ref: str
    height: int


class SetPaneHeightResponse(BaseModel):
    ok: Literal[True]


class CaptureRequest(BaseModel):
    operation: Literal["capture"]
    pane_ref: str


class CaptureResponse(BaseModel):
    ok: Literal[True]
    exists: bool
    content: str | None = None

    @model_validator(mode="after")
    def _content_matches_exists(self) -> CaptureResponse:
        if self.exists and self.content is None:
            raise ValueError("exists=True requires content")
        if not self.exists and self.content is not None:
            raise ValueError("exists=False requires content=None")
        return self

"""`tmux_backend.py` stays RAW_IO (no `domain` import, per
`tests/test_infrastructure_boundary.py`) and only exposes a raw
`resize_context` query and a raw, unclamped `resize` primitive.
`infrastructure.terminal.operations` -- the one place in `infrastructure/
terminal` already permitted to import `domain` (TYPED_FILES) -- assembles
those two raw calls around `domain.service.pane_width.compute_clamped_
percent()`. These tests pin that assembly directly, independent of
`tests/test_tmux_backend.py` (which only covers the two raw primitives in
isolation) and `tests/test_usecase_spawn.py`/`tests/test_usecase_resize.py`
(which cover it indirectly, several layers up)."""

from typing import Any, ClassVar

import pytest

import sukuna.infrastructure.terminal.operations as operations_module
from sukuna.domain.entity.pane import SplitDirection
from sukuna.domain.mapper.terminal_mapper import WindowFrameResponse
from sukuna.errors import BackendError


class FakeTmuxBackend:
    """Records every request; answers `resize_context` with a scripted
    `(window_width, other_pane_count)` pair and `spawn` with a scripted
    pane/window ref -- everything else defaults to a plain `{"ok": True}`."""

    instances: ClassVar[list["FakeTmuxBackend"]] = []
    window_width: ClassVar[int] = 1000
    other_pane_count: ClassVar[int] = 0
    spawn_result: ClassVar[dict] = {"ok": True, "pane_ref": "%2", "window_ref": "@1"}
    resize_context_error: ClassVar[BaseException | None] = None
    resize_error: ClassVar[BaseException | None] = None
    pane_heights: ClassVar[list[int]] = [24, 24]
    pane_heights_error: ClassVar[BaseException | None] = None

    def __init__(self) -> None:
        self.calls: list[dict] = []
        type(self).instances.append(self)

    def _handle_spawn(self) -> dict:
        return dict(type(self).spawn_result)

    def _handle_resize_context(self) -> dict:
        context_error = type(self).resize_context_error
        if context_error is not None:
            raise context_error
        return {
            "ok": True,
            "window_width": type(self).window_width,
            "other_pane_count": type(self).other_pane_count,
        }

    def _handle_resize(self) -> dict:
        resize_error = type(self).resize_error
        if resize_error is not None:
            raise resize_error
        return {"ok": True}

    def _handle_pane_heights(self) -> dict:
        heights_error = type(self).pane_heights_error
        if heights_error is not None:
            raise heights_error
        return {"ok": True, "heights": list(type(self).pane_heights)}

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        operation = request["operation"]
        handlers = {
            "spawn": self._handle_spawn,
            "resize_context": self._handle_resize_context,
            "resize": self._handle_resize,
            "pane_heights": self._handle_pane_heights,
        }
        handler = handlers.get(operation)
        if handler is None:
            return {"ok": True}
        return handler()


class FakeIterm2Backend:
    """Never needs `resize_context` -- iTerm2's own clamping happens inside
    `iterm_script.py`'s own process, untouched by this card."""

    instances: ClassVar[list[Any]] = []
    spawn_result: ClassVar[dict] = {
        "ok": True,
        "pane_ref": "AAAA",
        "window_ref": "tab-1",
    }
    next_response: ClassVar[dict | None] = None

    def __init__(self) -> None:
        self.calls: list[dict] = []
        type(self).instances.append(self)

    def run(self, request: dict) -> dict:
        self.calls.append(dict(request))
        if request["operation"] == "spawn":
            return dict(type(self).spawn_result)
        next_response = type(self).next_response
        if next_response is not None:
            return dict(next_response)
        return {"ok": True}


@pytest.fixture(autouse=True)
def _fake_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTmuxBackend.instances = []
    FakeTmuxBackend.window_width = 1000
    FakeTmuxBackend.other_pane_count = 0
    FakeTmuxBackend.spawn_result = {"ok": True, "pane_ref": "%2", "window_ref": "@1"}
    FakeTmuxBackend.resize_context_error = None
    FakeTmuxBackend.resize_error = None
    FakeTmuxBackend.pane_heights = [24, 24]
    FakeTmuxBackend.pane_heights_error = None
    FakeIterm2Backend.instances = []
    FakeIterm2Backend.next_response = None
    monkeypatch.setattr(
        operations_module,
        "BACKENDS",
        {"tmux": FakeTmuxBackend, "iterm2": FakeIterm2Backend},
    )


def _ops(instances: list) -> list[str]:
    return [call["operation"] for instance in instances for call in instance.calls]


# -- resize() --


def test_resize_tmux_queries_context_then_issues_the_raw_resize_unclamped() -> None:
    response = operations_module.resize(backend="tmux", pane_ref="%1", percent=70)

    assert response.resize_warning is None
    assert _ops(FakeTmuxBackend.instances) == ["resize_context", "resize"]
    context_call, resize_call = (i.calls[0] for i in FakeTmuxBackend.instances)
    assert context_call == {"operation": "resize_context", "pane_ref": "%1"}
    assert resize_call == {"operation": "resize", "pane_ref": "%1", "percent": 70}


def test_resize_tmux_clamps_and_reports_a_warning_when_context_is_narrow() -> None:
    FakeTmuxBackend.window_width = 40
    FakeTmuxBackend.other_pane_count = 1

    response = operations_module.resize(backend="tmux", pane_ref="%1", percent=90)

    assert response.resize_warning is not None
    assert "clamped" in response.resize_warning
    _, resize_call = (i.calls[0] for i in FakeTmuxBackend.instances)
    assert resize_call["percent"] < 90


def test_resize_iterm2_never_queries_resize_context() -> None:
    response = operations_module.resize(backend="iterm2", pane_ref="AAAA", percent=70)

    assert response.resize_warning is None
    assert _ops(FakeIterm2Backend.instances) == ["resize"]
    assert FakeTmuxBackend.instances == []


def test_resize_tmux_propagates_a_resize_context_failure_uncaught() -> None:
    """Unlike `spawn()`'s post-split application (best-effort, see below),
    a direct `resize()` call (`sukuna resize`, the self-resize hook) lets a
    backend failure propagate -- exactly as it did before this card, when
    the same query lived inside `tmux_backend.py`'s own
    `_resize_width_with_safety_check()`."""
    FakeTmuxBackend.resize_context_error = BackendError("pane vanished")

    with pytest.raises(BackendError, match="pane vanished"):
        operations_module.resize(backend="tmux", pane_ref="%1", percent=70)


def test_resize_tmux_rejects_a_zero_window_width_as_a_backend_error() -> None:
    """An unvalidated `window_width=0` must not reach
    `compute_clamped_percent()` and raise a bare `ZeroDivisionError` -- it
    must translate to the `BackendError` every other invalid backend
    response produces."""
    FakeTmuxBackend.window_width = 0
    FakeTmuxBackend.other_pane_count = 1

    with pytest.raises(BackendError, match="ResizeContextResponse"):
        operations_module.resize(backend="tmux", pane_ref="%1", percent=70)


def test_resize_tmux_rejects_a_negative_window_width_as_a_backend_error() -> None:
    FakeTmuxBackend.window_width = -5
    FakeTmuxBackend.other_pane_count = 1

    with pytest.raises(BackendError, match="ResizeContextResponse"):
        operations_module.resize(backend="tmux", pane_ref="%1", percent=70)


def test_resize_tmux_rejects_a_negative_other_pane_count_as_a_backend_error() -> None:
    FakeTmuxBackend.window_width = 100
    FakeTmuxBackend.other_pane_count = -1

    with pytest.raises(BackendError, match="ResizeContextResponse"):
        operations_module.resize(backend="tmux", pane_ref="%1", percent=70)


# -- equalize() --


def test_equalize_tmux_reads_heights_then_sets_all_but_the_last_member() -> None:
    """The average-height/last-member-excluded behavior (heights
    [10, 20, 30] -> `-y 20` on the first two members only, never the last)
    is assembled here, at the layer that assembles the raw `pane_heights`
    read and `set_pane_height` writes around
    `domain.service.pane_height.equalize_heights`."""
    FakeTmuxBackend.pane_heights = [10, 20, 30]

    operations_module.equalize(backend="tmux", column_pane_refs=["%1", "%2", "%3"])

    assert _ops(FakeTmuxBackend.instances) == [
        "pane_heights",
        "set_pane_height",
        "set_pane_height",
    ]
    heights_call, first_set, second_set = (
        instance.calls[0] for instance in FakeTmuxBackend.instances
    )
    assert heights_call == {
        "operation": "pane_heights",
        "column_pane_refs": ["%1", "%2", "%3"],
    }
    assert first_set == {
        "operation": "set_pane_height",
        "pane_ref": "%1",
        "height": 20,
    }
    assert second_set == {
        "operation": "set_pane_height",
        "pane_ref": "%2",
        "height": 20,
    }


def test_equalize_tmux_floors_a_non_integral_average() -> None:
    FakeTmuxBackend.pane_heights = [10, 21]

    operations_module.equalize(backend="tmux", column_pane_refs=["%1", "%2"])

    _, set_call = (instance.calls[0] for instance in FakeTmuxBackend.instances)
    assert set_call == {"operation": "set_pane_height", "pane_ref": "%1", "height": 15}


def test_equalize_iterm2_sends_the_single_equalize_wire_op() -> None:
    """iTerm2 keeps the one-shot `equalize` op: its script computes the
    intentional mirror (`iterm_script.compute_equalize_target`) in-process,
    pinned against the domain formula by tests/test_equalize_parity.py."""
    operations_module.equalize(
        backend="iterm2", column_pane_refs=["AAAA", "BBBB", "CCCC"]
    )

    assert _ops(FakeIterm2Backend.instances) == ["equalize"]
    assert FakeIterm2Backend.instances[0].calls[0] == {
        "operation": "equalize",
        "column_pane_refs": ["AAAA", "BBBB", "CCCC"],
    }
    assert FakeTmuxBackend.instances == []


def test_equalize_tmux_propagates_a_pane_heights_failure_uncaught() -> None:
    """Callers (`usecase.spawn_shared.equalize_new_pane`, `usecase.close`)
    already downgrade any equalize `BackendError` to `equalize_warning`;
    the assembly itself stays transparent, exactly as the same failure did
    when the whole loop lived inside `tmux_backend.py`."""
    FakeTmuxBackend.pane_heights_error = BackendError("pane vanished")

    with pytest.raises(BackendError, match="pane vanished"):
        operations_module.equalize(backend="tmux", column_pane_refs=["%1", "%2"])


# -- spawn() --


def test_spawn_tmux_never_sends_active_pane_width_in_the_wire_request() -> None:
    operations_module.spawn(
        backend="tmux",
        command="claude",
        anchor_pane_ref="%1",
        split_direction=SplitDirection.HORIZONTAL,
        active_pane_width=70,
    )

    spawn_call = FakeTmuxBackend.instances[0].calls[0]
    assert "active_pane_width" not in spawn_call


def test_spawn_iterm2_never_sends_active_pane_width_in_the_wire_request() -> None:
    result = operations_module.spawn(
        backend="iterm2",
        command="claude",
        anchor_pane_ref="AAAA",
        split_direction=SplitDirection.HORIZONTAL,
        active_pane_width=70,
    )

    spawn_call = FakeIterm2Backend.instances[0].calls[0]
    assert "active_pane_width" not in spawn_call
    assert result.pending_active_pane_width == 70


def test_spawn_tmux_defers_active_pane_width_to_a_pending_follow_up() -> None:
    """tmux applies `active_pane_width` the same way iTerm2 does -- it
    always hands the percent back via `pending_active_pane_width` for the
    caller (`usecase.spawn_shared.apply_pending_active_pane_width`) to
    apply as its own follow-up `resize()`. `spawn()` itself never issues a
    `resize_context`/`resize` call (those are pinned, still assembled
    around the clamp policy, by the `resize()` tests above -- the
    clamp/warn-vs-fail coverage for the spawn path lives in
    `tests/test_usecase_spawn.py`, at the layer that owns the decision)."""
    result = operations_module.spawn(
        backend="tmux",
        command="claude",
        anchor_pane_ref="%1",
        split_direction=SplitDirection.HORIZONTAL,
        active_pane_width=70,
    )

    assert result.pending_active_pane_width == 70
    assert result.result.resize_warning is None
    assert _ops(FakeTmuxBackend.instances) == ["spawn"]


def test_spawn_tmux_reports_no_pending_width_when_none_requested() -> None:
    result = operations_module.spawn(
        backend="tmux",
        command="claude",
        anchor_pane_ref="%1",
        split_direction=SplitDirection.VERTICAL,
        active_pane_width=None,
    )

    assert result.pending_active_pane_width is None
    assert _ops(FakeTmuxBackend.instances) == ["spawn"]


# -- set_window_frame() --


def test_set_window_frame_iterm2_rejects_a_malformed_response_as_a_backend_error() -> (
    None
):
    """Every operation validates the backend's raw response through
    `parse_backend_response()` first, `set_window_frame()` included, even
    ones that discard the parsed result (like `select_pane()`). This pins
    that validation the same way the sibling `resize_context` tests above
    pin theirs (`match="SetWindowFrameResponse"` against the translated
    `BackendError`)."""
    FakeIterm2Backend.next_response = {"ok": False}

    with pytest.raises(BackendError, match="SetWindowFrameResponse"):
        operations_module.set_window_frame(
            backend="iterm2",
            window_ref="win-1",
            frame=WindowFrameResponse(ok=True, x=0.0, y=0.0, width=1.0, height=1.0),
        )

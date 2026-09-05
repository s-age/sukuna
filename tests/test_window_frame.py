from sukuna.domain.mapper.terminal_mapper import WindowFrameResponse
from sukuna.errors import BackendError
from sukuna.infrastructure.terminal import operations as terminal_ops
from sukuna.usecase.window_frame import preserve_window_frame

# -- window frame save/restore, shared by usecase/close.py,
# usecase/spawn.py, and usecase/resize.py. --

FRAME = WindowFrameResponse(ok=True, x=10.0, y=20.0, width=300.0, height=400.0)


def test_preserve_window_frame_reads_before_and_restores_after_the_block(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_get(**_: object) -> WindowFrameResponse:
        calls.append("get")
        return FRAME

    monkeypatch.setattr(terminal_ops, "get_window_frame", fake_get)
    monkeypatch.setattr(
        terminal_ops, "set_window_frame", lambda **_: calls.append("set")
    )

    with preserve_window_frame(backend="iterm2", window_ref="win-1"):
        calls.append("body")

    assert calls == ["get", "body", "set"]


def test_preserve_window_frame_forwards_the_saved_frame_and_window_ref_to_the_restore(
    monkeypatch,
) -> None:
    restore_calls: list[dict] = []

    monkeypatch.setattr(terminal_ops, "get_window_frame", lambda **_: FRAME)
    monkeypatch.setattr(
        terminal_ops,
        "set_window_frame",
        lambda **kwargs: restore_calls.append(kwargs),
    )

    with preserve_window_frame(backend="iterm2", window_ref="win-1"):
        pass

    assert restore_calls == [
        {"backend": "iterm2", "window_ref": "win-1", "frame": FRAME}
    ]


def test_preserve_window_frame_restores_even_when_the_block_raises(monkeypatch) -> None:
    restore_calls: list[dict] = []

    monkeypatch.setattr(terminal_ops, "get_window_frame", lambda **_: FRAME)
    monkeypatch.setattr(
        terminal_ops,
        "set_window_frame",
        lambda **kwargs: restore_calls.append(kwargs),
    )

    class Boom(Exception):
        pass

    try:
        with preserve_window_frame(backend="iterm2", window_ref="win-1"):
            raise Boom
    except Boom:
        pass

    assert len(restore_calls) == 1


def test_preserve_window_frame_skips_restore_when_the_read_fails(monkeypatch) -> None:
    restore_calls: list[dict] = []

    def failing_get(**_: object) -> WindowFrameResponse:
        raise BackendError("no frame")

    monkeypatch.setattr(terminal_ops, "get_window_frame", failing_get)
    monkeypatch.setattr(
        terminal_ops,
        "set_window_frame",
        lambda **kwargs: restore_calls.append(kwargs),
    )

    with preserve_window_frame(backend="iterm2", window_ref="win-1"):
        pass

    assert restore_calls == []


def test_preserve_window_frame_swallows_a_restore_failure(monkeypatch) -> None:
    monkeypatch.setattr(terminal_ops, "get_window_frame", lambda **_: FRAME)

    def failing_set(**_: object) -> None:
        raise BackendError("could not restore")

    monkeypatch.setattr(terminal_ops, "set_window_frame", failing_set)

    with preserve_window_frame(backend="iterm2", window_ref="win-1"):
        pass  # must not raise despite the restore failure above

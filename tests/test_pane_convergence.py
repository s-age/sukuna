from sukuna.domain.mapper.terminal_mapper import ResizeResponse
from sukuna.domain.service.resize_convergence import MAX_RESIZE_ATTEMPTS
from sukuna.infrastructure.terminal import operations as terminal_ops
from sukuna.usecase.pane_convergence import converge_active_pane_width

# -- active_pane_width convergence retry loop, shared by
# usecase/spawn.py (iTerm2 post-split percent application) and
# usecase/resize.py (`sukuna resize`, self-resize hook). --


def test_converge_active_pane_width_returns_none_when_the_first_attempt_is_within_tolerance(
    monkeypatch,
) -> None:
    calls: list[int] = []

    def fake_resize(*, backend: str, pane_ref: str, percent: int) -> ResizeResponse:
        calls.append(percent)
        return ResizeResponse(ok=True, achieved_percent=68)

    monkeypatch.setattr(terminal_ops, "resize", fake_resize)

    response, warning = converge_active_pane_width(
        backend="iterm2", pane_ref="AAAA", percent=70
    )

    assert warning is None
    assert response.achieved_percent == 68
    assert calls == [70]


def test_converge_active_pane_width_retries_until_within_tolerance(monkeypatch) -> None:
    sequence = iter([40, 55, 66, 68])

    def fake_resize(*, backend: str, pane_ref: str, percent: int) -> ResizeResponse:
        return ResizeResponse(ok=True, achieved_percent=next(sequence))

    monkeypatch.setattr(terminal_ops, "resize", fake_resize)

    _, warning = converge_active_pane_width(
        backend="iterm2", pane_ref="AAAA", percent=70
    )

    assert warning is None


def test_converge_active_pane_width_gives_up_after_max_attempts_and_reports_a_warning(
    monkeypatch,
) -> None:
    attempts = {"n": 0}

    def fake_resize(*, backend: str, pane_ref: str, percent: int) -> ResizeResponse:
        attempts["n"] += 1
        return ResizeResponse(ok=True, achieved_percent=10)  # never converges toward 70

    monkeypatch.setattr(terminal_ops, "resize", fake_resize)

    _, warning = converge_active_pane_width(
        backend="iterm2", pane_ref="AAAA", percent=70
    )

    assert attempts["n"] == MAX_RESIZE_ATTEMPTS
    assert warning is not None
    assert "70%" in warning
    assert "10%" in warning
    assert str(MAX_RESIZE_ATTEMPTS) in warning


def test_converge_active_pane_width_stops_immediately_when_the_backend_reports_no_achieved_percent(
    monkeypatch,
) -> None:
    calls: list[int] = []

    def fake_resize(*, backend: str, pane_ref: str, percent: int) -> ResizeResponse:
        calls.append(percent)
        return ResizeResponse(ok=True, achieved_percent=None)

    monkeypatch.setattr(terminal_ops, "resize", fake_resize)

    _, warning = converge_active_pane_width(
        backend="iterm2", pane_ref="AAAA", percent=70
    )

    assert warning is None
    assert len(calls) == 1


def test_converge_active_pane_width_returns_the_last_response_for_callers_that_need_it(
    monkeypatch,
) -> None:
    """usecase/resize.py reads `resize_warning`/`achieved_percent` off the
    returned response directly -- e.g. a backend-side clamp warning is
    orthogonal to this loop's own could-not-converge warning."""

    def fake_resize(*, backend: str, pane_ref: str, percent: int) -> ResizeResponse:
        return ResizeResponse(
            ok=True, achieved_percent=68, resize_warning="clamped to 68%"
        )

    monkeypatch.setattr(terminal_ops, "resize", fake_resize)

    response, warning = converge_active_pane_width(
        backend="iterm2", pane_ref="AAAA", percent=70
    )

    assert warning is None
    assert response.resize_warning == "clamped to 68%"


# -- downward-correction on overshoot --


def test_converge_active_pane_width_corrects_downward_after_an_overshoot(
    monkeypatch,
) -> None:
    """requested=70, tolerance=3. First attempt overshoots to 80 (diff=10) ->
    next request should be the sign-flipped correction: 70 - (80-70) = 60.
    Second attempt lands exactly on 70 -> converges, no further correction
    needed."""
    achieved_sequence = iter([80, 70])
    requested_percents: list[int] = []

    def fake_resize(*, backend: str, pane_ref: str, percent: int) -> ResizeResponse:
        requested_percents.append(percent)
        return ResizeResponse(ok=True, achieved_percent=next(achieved_sequence))

    monkeypatch.setattr(terminal_ops, "resize", fake_resize)

    _, warning = converge_active_pane_width(
        backend="iterm2", pane_ref="AAAA", percent=70
    )

    assert warning is None
    assert requested_percents == [70, 60]


def test_converge_active_pane_width_undershoot_keeps_retrying_the_original_requested_percent(
    monkeypatch,
) -> None:
    """requested=70; achieved stays below 70-tolerance for two attempts --
    undershoot is left uncorrected, so every request should still ask for
    the original 70, not some corrected-upward value."""
    achieved_sequence = iter([40, 55, 69])
    requested_percents: list[int] = []

    def fake_resize(*, backend: str, pane_ref: str, percent: int) -> ResizeResponse:
        requested_percents.append(percent)
        return ResizeResponse(ok=True, achieved_percent=next(achieved_sequence))

    monkeypatch.setattr(terminal_ops, "resize", fake_resize)

    _, warning = converge_active_pane_width(
        backend="iterm2", pane_ref="AAAA", percent=70
    )

    assert warning is None
    assert requested_percents == [70, 70, 70]


def test_converge_active_pane_width_clamps_the_correction_to_0_and_100(
    monkeypatch,
) -> None:
    """An extreme overshoot must not push the corrected request outside
    [0, 100] -- e.g. requested=10, achieved=95 would otherwise compute
    10 - (95-10) = -75."""
    requested_percents: list[int] = []

    def fake_resize(*, backend: str, pane_ref: str, percent: int) -> ResizeResponse:
        requested_percents.append(percent)
        return ResizeResponse(
            ok=True, achieved_percent=95
        )  # never converges, forces max_attempts

    monkeypatch.setattr(terminal_ops, "resize", fake_resize)

    converge_active_pane_width(backend="iterm2", pane_ref="AAAA", percent=10)

    assert all(0 <= p <= 100 for p in requested_percents)
    assert requested_percents[1] == 0  # clamped, not -75
    assert len(requested_percents) == MAX_RESIZE_ATTEMPTS


def test_converge_active_pane_width_reoscillation_still_gives_up_after_max_attempts(
    monkeypatch,
) -> None:
    """A correction that itself overshoots the other way (undershoot after
    correction, per the owner-ruled formula) must not retry forever --
    the bounded loop still gives up and reports a warning."""
    achieved_sequence = iter([90, 90, 90, 90, 90, 90, 90, 90])  # never converges

    def fake_resize(*, backend: str, pane_ref: str, percent: int) -> ResizeResponse:
        return ResizeResponse(ok=True, achieved_percent=next(achieved_sequence))

    monkeypatch.setattr(terminal_ops, "resize", fake_resize)

    _, warning = converge_active_pane_width(
        backend="iterm2", pane_ref="AAAA", percent=70
    )

    assert warning is not None
    assert "70%" in warning
    assert "90%" in warning

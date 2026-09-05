"""Field-level validation on the pydantic wire-boundary models in
`domain/mapper/terminal_mapper.py` -- distinct from
`tests/test_terminal_operations.py`, which pins the assembly around these
models rather than the models' own value constraints."""

import pytest
from pydantic import ValidationError

from sukuna.domain.entity.pane import SplitDirection
from sukuna.domain.mapper.terminal_mapper import ResizeContextResponse, SpawnRequest

# -- SpawnRequest.split_direction --


def test_spawn_request_accepts_the_two_valid_split_directions() -> None:
    horizontal = SpawnRequest(
        operation="spawn",
        command="x",
        anchor_pane_ref=None,
        split_direction=SplitDirection.HORIZONTAL,
    )
    vertical = SpawnRequest(
        operation="spawn",
        command="x",
        anchor_pane_ref=None,
        split_direction=SplitDirection.VERTICAL,
    )

    assert horizontal.split_direction == "horizontal"
    assert vertical.split_direction == "vertical"


def test_spawn_request_rejects_a_typo_split_direction() -> None:
    """An unrecognized value must not survive construction -- falling
    through to `tmux_backend.py`'s `== "horizontal"` check would produce
    an unannounced vertical split."""
    with pytest.raises(ValidationError):
        SpawnRequest(
            operation="spawn",
            command="x",
            anchor_pane_ref=None,
            split_direction="horizonal",  # type: ignore[arg-type]
        )


# -- ResizeContextResponse.window_width / other_pane_count --


def test_resize_context_response_accepts_positive_width_and_nonnegative_count() -> None:
    response = ResizeContextResponse(ok=True, window_width=200, other_pane_count=0)

    assert response.window_width == 200
    assert response.other_pane_count == 0


@pytest.mark.parametrize("window_width", [0, -5])
def test_resize_context_response_rejects_a_non_positive_window_width(
    window_width: int,
) -> None:
    with pytest.raises(ValidationError):
        ResizeContextResponse(ok=True, window_width=window_width, other_pane_count=1)


def test_resize_context_response_rejects_a_negative_other_pane_count() -> None:
    with pytest.raises(ValidationError):
        ResizeContextResponse(ok=True, window_width=100, other_pane_count=-1)

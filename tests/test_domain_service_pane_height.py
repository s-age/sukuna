"""The equalize target-height formula (`sum(heights) // len(column)`) lives
in `domain.service.pane_height.equalize_heights`. These pin the formula's
numeric behavior directly; the "apply to the first N-1 members" loop stays
with the callers and is pinned by `tests/test_terminal_operations.py`
(tmux assembly) and the pre-existing behavior tests."""

from sukuna.domain.service.pane_height import equalize_heights


def test_target_is_the_floored_average_of_the_current_heights() -> None:
    assert equalize_heights([10, 20, 30]) == 20


def test_target_floors_a_non_integral_average() -> None:
    assert equalize_heights([10, 21]) == 15


def test_target_of_an_already_equal_column_is_the_shared_height() -> None:
    assert equalize_heights([24, 24, 24]) == 24


def test_target_handles_zero_heights_like_the_original_expression() -> None:
    assert equalize_heights([0, 0]) == 0

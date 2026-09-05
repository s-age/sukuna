from pathlib import Path

import pytest

from sukuna.domain.mapper.command_mapper import (
    validate_name,
    validate_role,
    worker_command,
)
from sukuna.errors import ValidationError


def test_worker_command_quotes_worktree_and_name() -> None:
    command = worker_command(
        worktree=Path("/tmp/a repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/bin/zsh",
    )

    assert command == (
        "/bin/zsh -lc 'cd '\"'\"'/tmp/a repo'\"'\"' && exec claude -n ccw-project-a1b2c3-review-1'"
    )


def test_worker_command_uses_the_given_shell() -> None:
    command = worker_command(
        worktree=Path("/tmp/repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/usr/bin/bash",
    )

    assert command.startswith("/usr/bin/bash -lc ")


def test_worker_command_quotes_a_shell_path_containing_a_space() -> None:
    command = worker_command(
        worktree=Path("/tmp/repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/opt/my shell/zsh",
    )

    assert command.startswith("'/opt/my shell/zsh' -lc ")


def test_worker_command_with_explicit_model_none_matches_the_no_model_output() -> None:
    assert worker_command(
        worktree=Path("/tmp/a repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/bin/zsh",
        model=None,
    ) == worker_command(
        worktree=Path("/tmp/a repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/bin/zsh",
    )


def test_worker_command_appends_model_flag_when_given() -> None:
    command = worker_command(
        worktree=Path("/tmp/repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/bin/zsh",
        model="sonnet-5",
    )

    assert command == (
        "/bin/zsh -lc 'cd /tmp/repo && exec claude -n ccw-project-a1b2c3-review-1 "
        "--model sonnet-5'"
    )


@pytest.mark.parametrize("role", ["Review", "review_task", "", "x" * 33])
def test_role_must_be_a_small_identifier(role: str) -> None:
    with pytest.raises(ValidationError):
        validate_role(role)


@pytest.mark.parametrize("name", ["Worker", "has space", "-starts-with-hyphen"])
def test_name_must_be_a_small_identifier(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_name(name)

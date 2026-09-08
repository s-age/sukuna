import shlex
from pathlib import Path

import pytest

from sukuna.domain.mapper.command_mapper import (
    resume_command,
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


def test_resume_command_with_no_model_and_no_permission_mode_matches_omitted_kwargs() -> (
    None
):
    assert resume_command(
        worktree=Path("/tmp/repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/bin/zsh",
    ) == resume_command(
        worktree=Path("/tmp/repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/bin/zsh",
        model=None,
        permission_mode=None,
    )


def test_resume_command_appends_model_flag_when_given() -> None:
    command = resume_command(
        worktree=Path("/tmp/repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/bin/zsh",
        model="sonnet",
    )

    assert command == (
        "/bin/zsh -lc 'cd /tmp/repo && exec claude --resume "
        "ccw-project-a1b2c3-review-1 --model sonnet'"
    )
    assert "--permission-mode" not in command


def test_resume_command_distinguishes_an_empty_string_model_from_none() -> None:
    """`shlex.quote()` re-escapes an already-quoted `''` when the whole
    inner script is quoted again for the outer shell wrap, so this checks
    the un-wrapped inner script (`shlex.split()`'s last token) rather than
    the final command string verbatim."""
    command = resume_command(
        worktree=Path("/tmp/repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/bin/zsh",
        model="",
    )
    inner = shlex.split(command)[-1]

    assert inner.endswith("--model ''")


def test_resume_command_appends_permission_mode_flag_when_given() -> None:
    command = resume_command(
        worktree=Path("/tmp/repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/bin/zsh",
        permission_mode="auto",
    )

    assert command == (
        "/bin/zsh -lc 'cd /tmp/repo && exec claude --resume "
        "ccw-project-a1b2c3-review-1 --permission-mode auto'"
    )
    assert "--model" not in command


def test_resume_command_appends_both_flags_when_given() -> None:
    command = resume_command(
        worktree=Path("/tmp/repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/bin/zsh",
        model="sonnet",
        permission_mode="auto",
    )

    assert command == (
        "/bin/zsh -lc 'cd /tmp/repo && exec claude --resume "
        "ccw-project-a1b2c3-review-1 --model sonnet --permission-mode auto'"
    )


def test_resume_command_with_explicit_none_matches_the_no_flags_output() -> None:
    with_explicit_none = resume_command(
        worktree=Path("/tmp/repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/bin/zsh",
        model=None,
        permission_mode=None,
    )
    without_kwargs = resume_command(
        worktree=Path("/tmp/repo"),
        name="ccw-project-a1b2c3-review-1",
        shell="/bin/zsh",
    )

    assert with_explicit_none == without_kwargs
    assert "--model" not in with_explicit_none
    assert "--permission-mode" not in with_explicit_none


def test_resume_command_still_validates_name_before_touching_model_or_permission_mode() -> (
    None
):
    with pytest.raises(ValidationError):
        resume_command(
            worktree=Path("/tmp/repo"),
            name="Not A Valid Name",
            shell="/bin/zsh",
            model="sonnet",
            permission_mode="auto",
        )


@pytest.mark.parametrize("role", ["Review", "review_task", "", "x" * 33])
def test_role_must_be_a_small_identifier(role: str) -> None:
    with pytest.raises(ValidationError):
        validate_role(role)


@pytest.mark.parametrize("name", ["Worker", "has space", "-starts-with-hyphen"])
def test_name_must_be_a_small_identifier(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_name(name)

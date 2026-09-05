import json
import os
import re
from pathlib import Path

import pytest

from sukuna.errors import ValidationError
from sukuna.infrastructure.claude_settings import (
    HOOK_EVENT,
    install_hooks,
    merge_hooks,
    read_settings_document,
)


def test_read_settings_document_returns_empty_dict_when_the_file_is_absent(
    tmp_path: Path,
) -> None:
    assert read_settings_document(tmp_path / "does-not-exist.json") == {}


def test_read_settings_document_returns_empty_dict_for_an_empty_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text("", encoding="utf-8")

    assert read_settings_document(path) == {}


def test_merge_hooks_does_not_mutate_its_input(tmp_path: Path) -> None:
    document = {"model": "opus"}

    merge_hooks(document)

    assert document == {"model": "opus"}


def test_merge_hooks_does_not_mutate_an_input_that_already_has_hooks() -> None:
    document = {
        "hooks": {
            HOOK_EVENT: [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}
            ]
        }
    }
    original = json.loads(json.dumps(document))

    merge_hooks(document)

    assert document == original


def test_merge_hooks_adds_the_matcher_group_to_an_empty_document() -> None:
    new_document, summary = merge_hooks({})

    matchers = {group["matcher"] for group in new_document["hooks"][HOOK_EVENT]}
    assert matchers == {"AskUserQuestion"}
    assert summary == {"AskUserQuestion": True}


def test_merge_hooks_preserves_unrelated_top_level_keys_and_matcher_groups() -> None:
    document = {
        "model": "opus",
        "permissions": {"allow": ["Bash(git *)"]},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Read|Edit|Write|Glob|Grep",
                    "hooks": [{"type": "command", "command": "x"}],
                }
            ],
            "PostToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "y"}]}
            ],
        },
    }

    new_document, _ = merge_hooks(document)

    assert new_document["model"] == "opus"
    assert new_document["permissions"] == {"allow": ["Bash(git *)"]}
    assert new_document["hooks"]["PostToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "y"}]}
    ]
    pre_tool_use_matchers = {
        group["matcher"] for group in new_document["hooks"]["PreToolUse"]
    }
    assert pre_tool_use_matchers == {
        "Read|Edit|Write|Glob|Grep",
        "AskUserQuestion",
    }
    original_group = next(
        group
        for group in new_document["hooks"]["PreToolUse"]
        if group["matcher"] == "Read|Edit|Write|Glob|Grep"
    )
    assert original_group["hooks"] == [{"type": "command", "command": "x"}]


def test_merge_hooks_is_idempotent_on_repeated_calls() -> None:
    once, _ = merge_hooks({})

    twice, summary = merge_hooks(once)

    assert twice == once
    assert summary == {"AskUserQuestion": False}


def test_merge_hooks_appends_to_an_existing_matcher_group_instead_of_duplicating_it() -> (
    None
):
    document = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "AskUserQuestion",
                    "hooks": [
                        {"type": "command", "command": "some-other-preexisting-hook"}
                    ],
                }
            ]
        }
    }

    new_document, summary = merge_hooks(document)

    groups = [
        g
        for g in new_document["hooks"]["PreToolUse"]
        if g["matcher"] == "AskUserQuestion"
    ]
    assert len(groups) == 1
    commands = [hook["command"] for hook in groups[0]["hooks"]]
    assert "some-other-preexisting-hook" in commands
    assert len(commands) == 2
    assert summary["AskUserQuestion"] is True


def test_install_hooks_writes_the_merged_document_to_disk(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "opus"}), encoding="utf-8")

    summary = install_hooks(path)

    assert summary == {"AskUserQuestion": True}
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["model"] == "opus"
    matchers = {group["matcher"] for group in on_disk["hooks"][HOOK_EVENT]}
    assert matchers == {"AskUserQuestion"}


def test_install_hooks_creates_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "settings.json"

    install_hooks(path)

    assert path.exists()


def test_install_hooks_run_twice_does_not_duplicate_entries(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"

    install_hooks(path)
    install_hooks(path)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    groups = on_disk["hooks"][HOOK_EVENT]
    assert len(groups) == 1
    for group in groups:
        assert len(group["hooks"]) == 1


def test_install_hooks_skips_the_write_when_every_hook_is_already_installed(
    tmp_path: Path,
) -> None:
    """A no-op run (every matcher already installed) must not touch the
    file at all -- settings.json is owned by Claude Code, not sukuna, and
    re-serializing it would normalize away a foreign formatting (here:
    4-space indent, no trailing newline) that sukuna must not disturb."""
    path = tmp_path / "settings.json"
    already_installed, _ = merge_hooks({})
    original_bytes = json.dumps(already_installed, indent=4).encode("utf-8")
    path.write_bytes(original_bytes)
    original_mtime_ns = path.stat().st_mtime_ns

    summary = install_hooks(path)

    assert summary == {"AskUserQuestion": False}
    assert path.read_bytes() == original_bytes
    assert path.stat().st_mtime_ns == original_mtime_ns


def test_read_settings_document_raises_validation_error_on_invalid_json(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ValidationError, match=re.escape(str(path))):
        read_settings_document(path)


def test_read_settings_document_raises_validation_error_when_root_is_not_an_object(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValidationError, match=re.escape(str(path))):
        read_settings_document(path)


def test_merge_hooks_raises_validation_error_when_hooks_is_null() -> None:
    with pytest.raises(ValidationError, match="hooks"):
        merge_hooks({"hooks": None})


def test_merge_hooks_raises_validation_error_when_pre_tool_use_is_a_dict() -> None:
    with pytest.raises(ValidationError, match=HOOK_EVENT):
        merge_hooks({"hooks": {HOOK_EVENT: {"matcher": "Bash"}}})


def test_merge_hooks_raises_validation_error_when_a_matcher_group_is_not_an_object() -> (
    None
):
    document = {"hooks": {HOOK_EVENT: ["oops"]}}

    with pytest.raises(ValidationError, match=HOOK_EVENT):
        merge_hooks(document)


def test_merge_hooks_raises_validation_error_when_matcher_group_hooks_is_not_a_list() -> (
    None
):
    document = {
        "hooks": {HOOK_EVENT: [{"matcher": "Bash", "hooks": {"type": "command"}}]}
    }

    with pytest.raises(ValidationError, match="Bash"):
        merge_hooks(document)


def test_merge_hooks_raises_validation_error_when_a_hook_entry_is_not_an_object() -> (
    None
):
    document = {
        "hooks": {HOOK_EVENT: [{"matcher": "AskUserQuestion", "hooks": ["oops"]}]}
    }

    with pytest.raises(ValidationError, match="AskUserQuestion"):
        merge_hooks(document)


def test_install_hooks_fsyncs_the_temp_file_before_the_atomic_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fsync has no on-disk signature a full-path test could otherwise
    observe, so the only way to guard it against a silent future removal
    is to assert the call itself (same monkeypatch discipline as
    `subprocess.run` elsewhere in this suite)."""
    real_fsync = os.fsync
    calls: list[int] = []

    def fake_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fake_fsync)
    path = tmp_path / "settings.json"

    install_hooks(path)

    assert len(calls) == 1

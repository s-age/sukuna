import os
import tomllib
from pathlib import Path

import pytest

from sukuna.errors import ValidationError
from sukuna.infrastructure.registry import sukuna_state_dir
from sukuna.infrastructure.settings import (
    DEFAULT_ACTIVE_PANE_WIDTH,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SHOULD_FOCUS_WORKER,
    DEFAULT_TUI_ENABLED,
    load_active_pane_width,
    load_preferred_backend,
    load_retention_days,
    load_should_focus_worker,
    load_tui_enabled,
    settings_path,
    validate_preferred_backend,
    validate_retention_days,
    validate_should_focus_worker,
    validate_tui_enabled,
    write_active_pane_width,
    write_preferred_backend,
    write_retention_days,
    write_should_focus_worker,
    write_tui_enabled,
)


def test_settings_path_defaults_under_application_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    path = settings_path()

    assert (
        path
        == Path.home() / "Library" / "Application Support" / "sukuna" / "setting.toml"
    )


def test_settings_path_honors_xdg_state_home_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert settings_path() == tmp_path / "sukuna" / "setting.toml"


def test_settings_path_is_derived_from_sukuna_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the dedup: registry.py and settings.py must stay pointed at
    the same directory. A future re-inlining of settings.py's own
    XDG_STATE_HOME expression that drifted from `sukuna_state_dir()` would
    fail this alongside `test_registry_default_path_is_derived_from_sukuna_state_dir`."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert settings_path() == sukuna_state_dir() / "setting.toml"


def test_load_active_pane_width_returns_the_default_when_the_file_is_absent(
    tmp_path: Path,
) -> None:
    assert (
        load_active_pane_width(tmp_path / "does-not-exist.toml")
        == DEFAULT_ACTIVE_PANE_WIDTH
    )


def test_load_active_pane_width_returns_the_default_when_the_key_is_unset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('some_other_key = "value"\n', encoding="utf-8")

    assert load_active_pane_width(path) == DEFAULT_ACTIVE_PANE_WIDTH


def test_load_active_pane_width_reads_the_configured_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text("active_pane_width = 70\n", encoding="utf-8")

    assert load_active_pane_width(path) == 70


def test_load_active_pane_width_rejects_malformed_toml(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text("this is not [ valid toml\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_active_pane_width(path)


def test_load_active_pane_width_rejects_an_out_of_range_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text("active_pane_width = 100\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_active_pane_width(path)


def test_load_active_pane_width_rejects_a_non_integer_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('active_pane_width = "70"\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        load_active_pane_width(path)


def test_write_active_pane_width_creates_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "setting.toml"

    write_active_pane_width(65, path)

    assert load_active_pane_width(path) == 65


def test_write_active_pane_width_overwrites_an_existing_value_in_place(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"
    path.write_text(
        "some_other_key = 1\nactive_pane_width = 40\nanother_key = 2\n",
        encoding="utf-8",
    )

    write_active_pane_width(60, path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ["some_other_key = 1", "active_pane_width = 60", "another_key = 2"]


def test_write_active_pane_width_preserves_unrelated_keys(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('some_other_key = "value"\n', encoding="utf-8")

    write_active_pane_width(30, path)

    content = path.read_text(encoding="utf-8")
    assert 'some_other_key = "value"' in content
    assert load_active_pane_width(path) == 30


def test_write_active_pane_width_inserts_before_a_table_header_when_the_key_is_absent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"
    path.write_text("[some_table]\nfoo = 1\n", encoding="utf-8")

    write_active_pane_width(45, path)

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert lines[0] == "active_pane_width = 45"
    assert lines[1] == "[some_table]"
    assert lines[2] == "foo = 1"
    assert load_active_pane_width(path) == 45


def test_write_active_pane_width_rejects_an_out_of_range_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"

    with pytest.raises(ValidationError):
        write_active_pane_width(100, path)

    assert not path.exists()


def test_validate_preferred_backend_accepts_tmux_and_iterm2() -> None:
    assert validate_preferred_backend("tmux") == "tmux"
    assert validate_preferred_backend("iterm2") == "iterm2"


def test_validate_preferred_backend_rejects_anything_else() -> None:
    with pytest.raises(ValidationError):
        validate_preferred_backend("screen")


def test_load_preferred_backend_returns_none_when_the_file_is_absent(
    tmp_path: Path,
) -> None:
    assert load_preferred_backend(tmp_path / "does-not-exist.toml") is None


def test_load_preferred_backend_returns_none_when_the_key_is_unset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('some_other_key = "value"\n', encoding="utf-8")

    assert load_preferred_backend(path) is None


def test_load_preferred_backend_reads_the_configured_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('preferred_backend = "iterm2"\n', encoding="utf-8")

    assert load_preferred_backend(path) == "iterm2"


def test_load_preferred_backend_rejects_malformed_toml(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text("this is not [ valid toml\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_preferred_backend(path)


def test_load_preferred_backend_rejects_an_unknown_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('preferred_backend = "screen"\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        load_preferred_backend(path)


def test_write_preferred_backend_creates_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "setting.toml"

    write_preferred_backend("tmux", path)

    assert load_preferred_backend(path) == "tmux"


def test_write_preferred_backend_overwrites_an_existing_value_in_place(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"
    path.write_text(
        'some_other_key = 1\npreferred_backend = "tmux"\nanother_key = 2\n',
        encoding="utf-8",
    )

    write_preferred_backend("iterm2", path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "some_other_key = 1",
        'preferred_backend = "iterm2"',
        "another_key = 2",
    ]


def test_write_preferred_backend_preserves_unrelated_keys(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('some_other_key = "value"\n', encoding="utf-8")

    write_preferred_backend("tmux", path)

    content = path.read_text(encoding="utf-8")
    assert 'some_other_key = "value"' in content
    assert load_preferred_backend(path) == "tmux"


def test_write_preferred_backend_and_active_pane_width_coexist_in_the_same_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"

    write_active_pane_width(60, path)
    write_preferred_backend("tmux", path)

    assert load_active_pane_width(path) == 60
    assert load_preferred_backend(path) == "tmux"


def test_write_preferred_backend_rejects_an_unknown_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"

    with pytest.raises(ValidationError):
        write_preferred_backend("screen", path)

    assert not path.exists()


def test_validate_should_focus_worker_accepts_bools() -> None:
    assert validate_should_focus_worker(True) is True
    assert validate_should_focus_worker(False) is False


def test_validate_should_focus_worker_rejects_non_bool_values() -> None:
    with pytest.raises(ValidationError):
        validate_should_focus_worker("true")


def test_load_should_focus_worker_returns_the_default_when_the_file_is_absent(
    tmp_path: Path,
) -> None:
    assert (
        load_should_focus_worker(tmp_path / "does-not-exist.toml")
        == DEFAULT_SHOULD_FOCUS_WORKER
    )


def test_load_should_focus_worker_returns_the_default_when_the_key_is_unset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('some_other_key = "value"\n', encoding="utf-8")

    assert load_should_focus_worker(path) == DEFAULT_SHOULD_FOCUS_WORKER


def test_load_should_focus_worker_reads_the_configured_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text("should_focus_worker = true\n", encoding="utf-8")

    assert load_should_focus_worker(path) is True


def test_load_should_focus_worker_rejects_malformed_toml(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text("this is not [ valid toml\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_should_focus_worker(path)


def test_load_should_focus_worker_rejects_a_non_bool_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('should_focus_worker = "true"\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        load_should_focus_worker(path)


def test_write_should_focus_worker_creates_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "setting.toml"

    write_should_focus_worker(True, path)

    assert load_should_focus_worker(path) is True


def test_write_should_focus_worker_overwrites_an_existing_value_in_place(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"
    path.write_text(
        "some_other_key = 1\nshould_focus_worker = false\nanother_key = 2\n",
        encoding="utf-8",
    )

    write_should_focus_worker(True, path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "some_other_key = 1",
        "should_focus_worker = true",
        "another_key = 2",
    ]


def test_write_should_focus_worker_preserves_unrelated_keys(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('some_other_key = "value"\n', encoding="utf-8")

    write_should_focus_worker(False, path)

    content = path.read_text(encoding="utf-8")
    assert 'some_other_key = "value"' in content
    assert load_should_focus_worker(path) is False


def test_write_should_focus_worker_and_active_pane_width_coexist_in_the_same_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"

    write_active_pane_width(60, path)
    write_should_focus_worker(True, path)

    assert load_active_pane_width(path) == 60
    assert load_should_focus_worker(path) is True


def test_validate_tui_enabled_accepts_bools() -> None:
    assert validate_tui_enabled(True) is True
    assert validate_tui_enabled(False) is False


def test_validate_tui_enabled_rejects_non_bool_values() -> None:
    with pytest.raises(ValidationError):
        validate_tui_enabled("true")


def test_load_tui_enabled_returns_the_default_when_the_file_is_absent(
    tmp_path: Path,
) -> None:
    assert load_tui_enabled(tmp_path / "does-not-exist.toml") == DEFAULT_TUI_ENABLED


def test_load_tui_enabled_returns_the_default_when_the_key_is_unset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('some_other_key = "value"\n', encoding="utf-8")

    assert load_tui_enabled(path) == DEFAULT_TUI_ENABLED


def test_load_tui_enabled_reads_the_configured_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text("tui_enabled = true\n", encoding="utf-8")

    assert load_tui_enabled(path) is True


def test_load_tui_enabled_rejects_malformed_toml(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text("this is not [ valid toml\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_tui_enabled(path)


def test_load_tui_enabled_rejects_a_non_bool_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('tui_enabled = "true"\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        load_tui_enabled(path)


def test_write_tui_enabled_creates_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "setting.toml"

    write_tui_enabled(True, path)

    assert load_tui_enabled(path) is True


def test_write_tui_enabled_overwrites_an_existing_value_in_place(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"
    path.write_text(
        "some_other_key = 1\ntui_enabled = false\nanother_key = 2\n",
        encoding="utf-8",
    )

    write_tui_enabled(True, path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "some_other_key = 1",
        "tui_enabled = true",
        "another_key = 2",
    ]


def test_write_tui_enabled_preserves_unrelated_keys(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('some_other_key = "value"\n', encoding="utf-8")

    write_tui_enabled(False, path)

    content = path.read_text(encoding="utf-8")
    assert 'some_other_key = "value"' in content
    assert load_tui_enabled(path) is False


def test_write_tui_enabled_and_active_pane_width_coexist_in_the_same_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"

    write_active_pane_width(60, path)
    write_tui_enabled(True, path)

    assert load_active_pane_width(path) == 60
    assert load_tui_enabled(path) is True


def test_write_key_fsyncs_the_temp_file_before_the_atomic_rename(
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
    path = tmp_path / "setting.toml"

    write_active_pane_width(60, path)

    assert len(calls) == 1


def test_write_preferred_backend_does_not_corrupt_a_multiline_nested_array(
    tmp_path: Path,
) -> None:
    """Writing a new key must not mistake a nested array's continuation
    line `  [1, 2],` for a table header and insert the new key inside the
    array."""
    path = tmp_path / "setting.toml"
    path.write_text(
        "matrix = [\n  [1, 2],\n  [3, 4],\n]\n",
        encoding="utf-8",
    )

    write_preferred_backend("tmux", path)

    content = path.read_text(encoding="utf-8")
    parsed = tomllib.loads(content)
    assert parsed["matrix"] == [[1, 2], [3, 4]]
    assert load_preferred_backend(path) == "tmux"


def test_write_preferred_backend_does_not_write_into_a_multiline_string(
    tmp_path: Path,
) -> None:
    """Writing a new key must not mistake a `[...]`-shaped line inside a
    multi-line basic string for a table header and insert the new key
    into the string's body."""
    path = tmp_path / "setting.toml"
    path.write_text(
        'active_pane_width = 50\nnote = """\n'
        '[section-like line inside a string]\n"""\n',
        encoding="utf-8",
    )

    write_preferred_backend("iterm2", path)

    content = path.read_text(encoding="utf-8")
    parsed = tomllib.loads(content)
    assert parsed["note"] == "[section-like line inside a string]\n"
    assert parsed["preferred_backend"] == "iterm2"
    assert load_preferred_backend(path) == "iterm2"
    assert load_active_pane_width(path) == 50


def test_write_active_pane_width_preserves_comments(tmp_path: Path) -> None:
    """`tomlkit` is round-trip safe: comments elsewhere in the file survive
    a write, same discipline the hand-rolled line editor also aimed for."""
    path = tmp_path / "setting.toml"
    path.write_text(
        "# a user comment\nactive_pane_width = 40  # inline note\n",
        encoding="utf-8",
    )

    write_active_pane_width(60, path)

    content = path.read_text(encoding="utf-8")
    assert "# a user comment" in content
    assert "# inline note" in content
    assert load_active_pane_width(path) == 60


def test_load_retention_days_returns_the_default_when_the_file_is_absent(
    tmp_path: Path,
) -> None:
    assert (
        load_retention_days(tmp_path / "does-not-exist.toml") == DEFAULT_RETENTION_DAYS
    )


def test_load_retention_days_returns_the_default_when_the_key_is_unset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('some_other_key = "value"\n', encoding="utf-8")

    assert load_retention_days(path) == DEFAULT_RETENTION_DAYS


def test_load_retention_days_reads_the_configured_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text("retention_days = 45\n", encoding="utf-8")

    assert load_retention_days(path) == 45


def test_load_retention_days_rejects_malformed_toml(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text("this is not [ valid toml\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_retention_days(path)


def test_load_retention_days_rejects_zero(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text("retention_days = 0\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_retention_days(path)


def test_load_retention_days_rejects_a_non_integer_value(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('retention_days = "45"\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        load_retention_days(path)


def test_write_retention_days_creates_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "setting.toml"

    write_retention_days(45, path)

    assert load_retention_days(path) == 45


def test_write_retention_days_overwrites_an_existing_value_in_place(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setting.toml"
    path.write_text(
        "some_other_key = 1\nretention_days = 40\nanother_key = 2\n",
        encoding="utf-8",
    )

    write_retention_days(60, path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ["some_other_key = 1", "retention_days = 60", "another_key = 2"]


def test_write_retention_days_preserves_unrelated_keys(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"
    path.write_text('some_other_key = "value"\n', encoding="utf-8")

    write_retention_days(30, path)

    content = path.read_text(encoding="utf-8")
    assert 'some_other_key = "value"' in content
    assert load_retention_days(path) == 30


def test_write_retention_days_rejects_zero(tmp_path: Path) -> None:
    path = tmp_path / "setting.toml"

    with pytest.raises(ValidationError):
        write_retention_days(0, path)

    assert not path.exists()


def test_validate_retention_days_accepts_a_positive_integer() -> None:
    assert validate_retention_days(30) == 30


def test_validate_retention_days_rejects_a_negative_value() -> None:
    with pytest.raises(ValidationError):
        validate_retention_days(-1)

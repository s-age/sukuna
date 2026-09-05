import io
import json
import sys
from pathlib import Path

import pytest

import sukuna.cli_human as cli_human_module
import sukuna.infrastructure.claude_settings as claude_settings_module
import sukuna.infrastructure.settings as settings_module
from sukuna.cli_human import _ask
from sukuna.cli_human import main_cli as main
from sukuna.errors import BackendError, ValidationError
from sukuna.infrastructure.terminal import resolver as resolver_module


class _FakeTTYStdin(io.StringIO):
    """`input()` falls back to `sys.stdin.readline()` once `sys.stdin` has
    been replaced, so a scripted `StringIO` works for feeding answers -- but
    a bare `StringIO.isatty()` is always `False`, so the TTY-requirement
    check needs a subclass that reports `True` instead."""

    def isatty(self) -> bool:
        return True


def _isolate_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """`sukuna-cli init` has no `--registry`-style override flag (it does not
    touch the worker registry at all), so the CLI-level test isolates it by
    patching the two path-resolving functions it calls instead -- the same
    real-filesystem risk `sukuna-cli init` always carries is exactly why the
    project CLAUDE.md forbids running it for real outside a test."""
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"
    monkeypatch.setattr(
        claude_settings_module, "claude_settings_path", lambda: settings_json_path
    )
    monkeypatch.setattr(settings_module, "settings_path", lambda: setting_toml_path)
    return settings_json_path, setting_toml_path


def _script(monkeypatch, *lines: str) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeTTYStdin("\n".join(lines) + "\n"))


def _both_backends_installed(monkeypatch) -> None:
    monkeypatch.setattr(resolver_module, "tmux_installed", lambda: True)
    monkeypatch.setattr(resolver_module, "iterm2_installed", lambda: True)


def _only_tmux_installed(monkeypatch) -> None:
    monkeypatch.setattr(resolver_module, "tmux_installed", lambda: True)
    monkeypatch.setattr(resolver_module, "iterm2_installed", lambda: False)


def _neither_backend_installed(monkeypatch) -> None:
    monkeypatch.setattr(resolver_module, "tmux_installed", lambda: False)
    monkeypatch.setattr(resolver_module, "iterm2_installed", lambda: False)


def _stub_node_status(
    monkeypatch, message: str = "Node.js v24.1.0 detected (>= 24, OK)"
) -> None:
    """`_prompt_enable_tui()` always shows `tui_node_status()`, which shells
    out to `node --version` -- stub it so tests never depend on the host's
    actual Node.js installation (same "mock the backend call" discipline as
    tmux/iTerm2 backend tests)."""
    monkeypatch.setattr(cli_human_module, "tui_node_status", lambda: message)


def _stub_npm_status(
    monkeypatch, message: str = "npm found on PATH: /usr/bin/npm"
) -> None:
    """Same discipline as `_stub_node_status()`, for the npm status line
    `_prompt_enable_tui()` also shows."""
    monkeypatch.setattr(cli_human_module, "npm_status_message", lambda: message)


def _stub_no_tui_build(monkeypatch) -> None:
    """`_prompt_enable_tui()` calls `should_run_tui_build()` after a
    yes/Enter answer, which (unstubbed) reaches the real `shutil.which`/
    `importlib.resources` checks -- harmless on its own, but a dev machine
    that has ever run `scripts/build_tui.sh` locally will have a real
    `_tui_source/` on disk, and combined with real npm on PATH this would
    make these tests actually invoke `npm ci`/`npm run build` (the exact
    "tests must mock external tools, never invoke them for real" violation
    the project CLAUDE.md forbids). Every test that answers yes/Enter to
    the TUI question must apply this."""
    monkeypatch.setattr(
        cli_human_module, "should_run_tui_build", lambda *, tui_enabled_answer: False
    )


def test_cli_init_requires_a_tty(tmp_path: Path, monkeypatch, capsys) -> None:
    settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # not a TTY

    exit_code = main(["init"])

    assert exit_code == 2
    assert not settings_json_path.exists()
    assert not setting_toml_path.exists()
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert "TTY" in output["message"]


def test_cli_init_errors_immediately_when_neither_backend_is_installed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _neither_backend_installed(monkeypatch)
    _script(monkeypatch)  # no answers needed; must fail before reading any

    exit_code = main(["init"])

    assert exit_code == 2
    assert not settings_json_path.exists()
    assert not setting_toml_path.exists()
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False


def test_cli_init_skips_backend_question_when_only_one_is_installed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _only_tmux_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "n", "", "", ""
    )  # width: skip, hooks: no, should_focus_worker: skip, tui: skip (defaults to enable), retention_days: skip -- no backend answer consumed

    exit_code = main(["init"])

    assert exit_code == 0
    assert not settings_json_path.exists()
    # Enter on the TUI question defaults to enabling it, so setting.toml
    # gets written even though every other item here was skipped.
    assert setting_toml_path.exists()
    assert settings_module.load_tui_enabled(setting_toml_path) is True
    output = json.loads(capsys.readouterr().out)
    assert "preferred_backend" not in output


def test_cli_init_asks_backend_question_when_both_are_installed_and_writes_the_choice(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "1", "", "n", "", "", ""
    )  # backend: tmux, width: skip, hooks: no, should_focus_worker: skip, tui: skip, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["preferred_backend"] == "tmux"
    assert settings_module.load_preferred_backend(setting_toml_path) == "tmux"


def test_cli_init_skipping_every_skippable_item_still_defaults_tui_to_enabled(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Pressing Enter through every other still-skippable item writes
    `tui_enabled=true` (Enter on the TUI question defaults to enabling
    it)."""
    settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "", "", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: skip (defaults to enable), retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    assert not settings_json_path.exists()
    assert setting_toml_path.exists()
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "ok": True,
        "settings_json_path": str(settings_json_path),
        "tui_enabled": True,
        "setting_toml_path": str(setting_toml_path),
    }
    assert settings_module.load_tui_enabled(setting_toml_path) is True


def test_cli_init_installing_hooks_shows_the_consent_text_before_asking(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "y", "", "", ""
    )  # backend: skip, width: skip, hooks: yes, should_focus_worker: skip, tui: skip, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    captured_err = capsys.readouterr().err
    consent_index = captured_err.index("sukuna-cli init will add one entry")
    question_index = captured_err.index("Install this hook")
    assert consent_index < question_index


def test_cli_init_installs_hooks_and_writes_active_pane_width_when_accepted(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "2", "70", "y", "y", "y", "45"
    )  # backend: iterm2, width: 70, hooks: yes, should_focus_worker: yes, tui: yes, retention_days: 45

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["installed_hooks"] == {"AskUserQuestion": True}
    assert output["active_pane_width"] == 70
    assert output["preferred_backend"] == "iterm2"
    assert output["should_focus_worker"] is True
    assert output["tui_enabled"] is True
    assert output["retention_days"] == 45
    assert settings_json_path.exists()
    assert setting_toml_path.exists()
    assert settings_module.load_tui_enabled(setting_toml_path) is True
    assert settings_module.load_retention_days(setting_toml_path) == 45


def test_cli_init_reprompts_on_an_out_of_range_active_pane_width(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "150", "70", "n", "", "", ""
    )  # backend: skip, width: invalid then valid, hooks: no, should_focus_worker: skip, tui: skip, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["active_pane_width"] == 70


def test_cli_init_retention_days_question_defaults_to_skip_on_enter(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "", "", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: skip (defaults to enable), retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert "retention_days" not in output


def test_cli_init_retention_days_question_reprompts_on_an_invalid_value(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "", "", "0", "45"
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: skip, retention_days: invalid (0) then valid (45)

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["retention_days"] == 45


def test_cli_init_should_focus_worker_question_defaults_to_skip_on_enter(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "", "", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: skip, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert "should_focus_worker" not in output


def test_cli_init_should_focus_worker_question_accepts_yes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "y", "", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: yes, tui: skip, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["should_focus_worker"] is True
    assert settings_module.load_should_focus_worker(setting_toml_path) is True


def test_cli_init_should_focus_worker_question_accepts_no(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "n", "", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: no, tui: skip, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["should_focus_worker"] is False
    assert settings_module.load_should_focus_worker(setting_toml_path) is False


def test_cli_init_should_focus_worker_question_reprompts_on_an_invalid_answer(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "maybe", "y", "", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: invalid then yes, tui: skip, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["should_focus_worker"] is True


def test_cli_init_aborts_cleanly_on_eof_at_the_first_prompt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _FakeTTYStdin(""))  # EOF immediately

    exit_code = main(["init"])

    assert exit_code == 2
    assert not settings_json_path.exists()
    assert not setting_toml_path.exists()
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"] == "VALIDATION_ERROR"
    assert "EOF" in output["message"]


def test_cli_init_aborts_cleanly_on_eof_partway_through_the_prompts(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _script(monkeypatch, "1", "70")  # backend: tmux, width: 70, then EOF on hooks

    exit_code = main(["init"])

    assert exit_code == 2
    assert not settings_json_path.exists()
    assert not setting_toml_path.exists()
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"] == "VALIDATION_ERROR"
    assert "EOF" in output["message"]


def test_cli_init_run_init_prompts_returns_six_items(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "1", "70", "y", "y", "y", "45"
    )  # backend: tmux, width: 70, hooks: yes, should_focus_worker: yes, tui: yes, retention_days: 45

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["preferred_backend"] == "tmux"
    assert output["active_pane_width"] == 70
    assert output["installed_hooks"] == {"AskUserQuestion": True}
    assert output["should_focus_worker"] is True
    assert output["tui_enabled"] is True
    assert output["retention_days"] == 45


def test_cli_init_tui_question_shows_consent_text_and_node_status_before_asking(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch, "Node.js v24.1.0 detected (>= 24, OK)")
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "", "", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: skip (defaults to enable), retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    captured_err = capsys.readouterr().err
    consent_index = captured_err.index("launches an interactive terminal UI")
    status_index = captured_err.index("Node.js v24.1.0 detected (>= 24, OK)")
    question_index = captured_err.index("Enable the interactive tree TUI")
    assert consent_index < status_index < question_index


def test_cli_init_tui_question_defaults_to_enabled_on_enter(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Pressing Enter on the TUI question opts in (writes
    `tui_enabled=true`), not skips -- unlike `_prompt_should_focus_worker`'s
    Enter-means-skip pattern."""
    _settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "", "", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: Enter (defaults to enable), retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tui_enabled"] is True
    assert settings_module.load_tui_enabled(setting_toml_path) is True


def test_cli_init_tui_question_accepts_yes(tmp_path: Path, monkeypatch, capsys) -> None:
    _settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "", "y", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: yes, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tui_enabled"] is True
    assert settings_module.load_tui_enabled(setting_toml_path) is True


def test_cli_init_tui_question_accepts_no(tmp_path: Path, monkeypatch, capsys) -> None:
    _settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "", "n", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: no, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tui_enabled"] is False
    assert settings_module.load_tui_enabled(setting_toml_path) is False


def test_cli_init_tui_question_reprompts_on_an_invalid_answer(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "", "maybe", "y", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: invalid then yes, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tui_enabled"] is True


def test_cli_init_tui_question_shows_npm_status_after_node_status(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch, "Node.js v24.1.0 detected (>= 24, OK)")
    _stub_npm_status(monkeypatch, "npm found on PATH: /usr/bin/npm")
    _stub_no_tui_build(monkeypatch)
    _script(
        monkeypatch, "", "", "n", "", "", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: skip (defaults to enable), retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    captured_err = capsys.readouterr().err
    node_index = captured_err.index("Node.js v24.1.0 detected")
    npm_index = captured_err.index("npm found on PATH")
    question_index = captured_err.index("Enable the interactive tree TUI")
    assert node_index < npm_index < question_index


def test_cli_init_tui_yes_with_build_gate_open_builds_the_bundle_once(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_npm_status(monkeypatch)
    monkeypatch.setattr(
        cli_human_module, "should_run_tui_build", lambda *, tui_enabled_answer: True
    )
    calls: list[None] = []
    monkeypatch.setattr(
        cli_human_module, "attempt_tui_build", lambda: calls.append(None)
    )
    _script(
        monkeypatch, "", "", "n", "", "y", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: yes, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    assert len(calls) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["tui_enabled"] is True


def test_cli_init_tui_build_failure_then_retry_succeeds(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_npm_status(monkeypatch)
    monkeypatch.setattr(
        cli_human_module, "should_run_tui_build", lambda *, tui_enabled_answer: True
    )
    outcomes = iter([BackendError("npm ci failed"), None])

    def fake_attempt() -> None:
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(cli_human_module, "attempt_tui_build", fake_attempt)
    _script(
        monkeypatch, "", "", "n", "", "y", "r", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: yes, retry-or-skip: retry, retention_days: skip

    exit_code = main(["init"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Building the tree TUI bundle failed" in captured.err
    assert next(outcomes, "exhausted") == "exhausted"
    output = json.loads(captured.out)
    assert output["tui_enabled"] is True


def test_cli_init_tui_build_failure_then_skip_still_completes_with_tui_enabled_true(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _settings_json_path, setting_toml_path = _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_npm_status(monkeypatch)
    monkeypatch.setattr(
        cli_human_module, "should_run_tui_build", lambda *, tui_enabled_answer: True
    )

    def fake_attempt() -> None:
        raise BackendError("npm run build failed")

    monkeypatch.setattr(cli_human_module, "attempt_tui_build", fake_attempt)
    _script(
        monkeypatch, "", "", "n", "", "y", "s", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: yes, retry-or-skip: skip, retention_days: skip

    exit_code = main(["init"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Skipping the tree TUI bundle build" in captured.err
    output = json.loads(captured.out)
    assert output["tui_enabled"] is True
    assert settings_module.load_tui_enabled(setting_toml_path) is True


def test_cli_init_tui_no_never_evaluates_the_build_gate(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_npm_status(monkeypatch)

    def fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not be called when the TUI question is declined")

    monkeypatch.setattr(cli_human_module, "should_run_tui_build", fail_if_called)
    monkeypatch.setattr(cli_human_module, "attempt_tui_build", fail_if_called)
    _script(
        monkeypatch, "", "", "n", "", "n", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: no, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tui_enabled"] is False


def test_cli_init_tui_yes_with_build_gate_closed_never_attempts_a_build(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    _both_backends_installed(monkeypatch)
    _stub_node_status(monkeypatch)
    _stub_npm_status(monkeypatch, "npm not found on PATH")
    monkeypatch.setattr(
        cli_human_module, "should_run_tui_build", lambda *, tui_enabled_answer: False
    )

    def fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("attempt_tui_build must not run when the gate is closed")

    monkeypatch.setattr(cli_human_module, "attempt_tui_build", fail_if_called)
    _script(
        monkeypatch, "", "", "n", "", "y", ""
    )  # backend: skip, width: skip, hooks: no, should_focus_worker: skip, tui: yes, retention_days: skip

    exit_code = main(["init"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tui_enabled"] is True


def test_ask_still_raises_validation_error_on_eof(monkeypatch) -> None:
    """Regression guard: adding a `KeyboardInterrupt` handler to
    `cli_shared.run()` must not touch the pre-existing EOF ->
    `ValidationError` path."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(ValidationError):
        _ask("prompt: ")

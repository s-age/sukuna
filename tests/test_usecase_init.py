import json
from pathlib import Path

import pytest

import sukuna.infrastructure.npm_runtime as npm_runtime_module
import sukuna.infrastructure.tui.launcher as tui_launcher_module
import sukuna.usecase.tui_bundle as tui_bundle_module
from sukuna.errors import BackendError, ValidationError
from sukuna.infrastructure.settings import (
    load_active_pane_width,
    load_preferred_backend,
    load_retention_days,
    load_should_focus_worker,
    load_tui_enabled,
)
from sukuna.usecase.init import (
    InitDecisions,
    attempt_tui_build,
    check_retention_days,
    init,
    should_run_tui_build,
)


def test_init_with_all_items_declined_writes_nothing(tmp_path: Path) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    result = init(
        InitDecisions(
            install_hooks=False, active_pane_width=None, preferred_backend=None
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    assert result == {"ok": True, "settings_json_path": str(settings_json_path)}
    assert not settings_json_path.exists()
    assert not setting_toml_path.exists()


def test_init_installs_hooks_when_requested(tmp_path: Path) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    result = init(
        InitDecisions(
            install_hooks=True, active_pane_width=None, preferred_backend=None
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    assert result["installed_hooks"] == {"AskUserQuestion": True}
    assert settings_json_path.exists()
    assert not setting_toml_path.exists()


def test_init_writes_active_pane_width_independently_of_hooks(tmp_path: Path) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    result = init(
        InitDecisions(
            install_hooks=False, active_pane_width=70, preferred_backend=None
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    assert result["active_pane_width"] == 70
    assert result["setting_toml_path"] == str(setting_toml_path)
    assert not settings_json_path.exists()
    assert load_active_pane_width(setting_toml_path) == 70


def test_init_writes_preferred_backend_independently_of_hooks_and_width(
    tmp_path: Path,
) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    result = init(
        InitDecisions(
            install_hooks=False, active_pane_width=None, preferred_backend="iterm2"
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    assert result["preferred_backend"] == "iterm2"
    assert result["setting_toml_path"] == str(setting_toml_path)
    assert not settings_json_path.exists()
    assert load_preferred_backend(setting_toml_path) == "iterm2"


def test_init_writes_all_three_items_together(tmp_path: Path) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    result = init(
        InitDecisions(
            install_hooks=True, active_pane_width=70, preferred_backend="tmux"
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    assert result["ok"] is True
    assert result["installed_hooks"] == {"AskUserQuestion": True}
    assert result["active_pane_width"] == 70
    assert result["preferred_backend"] == "tmux"
    assert settings_json_path.exists()
    assert load_active_pane_width(setting_toml_path) == 70
    assert load_preferred_backend(setting_toml_path) == "tmux"


def test_init_preserves_existing_settings_json_content(tmp_path: Path) -> None:
    settings_json_path = tmp_path / "settings.json"
    settings_json_path.write_text(
        json.dumps({"model": "opus", "theme": "dark"}), encoding="utf-8"
    )
    setting_toml_path = tmp_path / "setting.toml"

    init(
        InitDecisions(
            install_hooks=True, active_pane_width=None, preferred_backend=None
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    on_disk = json.loads(settings_json_path.read_text(encoding="utf-8"))
    assert on_disk["model"] == "opus"
    assert on_disk["theme"] == "dark"


def test_init_rejects_an_out_of_range_active_pane_width_before_writing_anything(
    tmp_path: Path,
) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    with pytest.raises(ValidationError):
        init(
            InitDecisions(
                install_hooks=True, active_pane_width=150, preferred_backend=None
            ),
            settings_json_path=settings_json_path,
            setting_toml_path=setting_toml_path,
        )

    assert not settings_json_path.exists()
    assert not setting_toml_path.exists()


def test_init_rejects_an_invalid_preferred_backend_before_writing_anything(
    tmp_path: Path,
) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    with pytest.raises(ValidationError):
        init(
            InitDecisions(
                install_hooks=True, active_pane_width=None, preferred_backend="screen"
            ),
            settings_json_path=settings_json_path,
            setting_toml_path=setting_toml_path,
        )

    assert not settings_json_path.exists()
    assert not setting_toml_path.exists()


def test_init_with_should_focus_worker_unset_writes_nothing(tmp_path: Path) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    result = init(
        InitDecisions(
            install_hooks=False,
            active_pane_width=None,
            preferred_backend=None,
            should_focus_worker=None,
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    assert "should_focus_worker" not in result
    assert not setting_toml_path.exists()


def test_init_writes_should_focus_worker_independently_of_other_items(
    tmp_path: Path,
) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    result = init(
        InitDecisions(
            install_hooks=False,
            active_pane_width=None,
            preferred_backend=None,
            should_focus_worker=True,
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    assert result["should_focus_worker"] is True
    assert result["setting_toml_path"] == str(setting_toml_path)
    assert not settings_json_path.exists()
    assert load_should_focus_worker(setting_toml_path) is True


def test_init_writes_all_six_items_together(tmp_path: Path) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    result = init(
        InitDecisions(
            install_hooks=True,
            active_pane_width=70,
            preferred_backend="tmux",
            should_focus_worker=True,
            tui_enabled=True,
            retention_days=45,
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    assert result["ok"] is True
    assert result["installed_hooks"] == {"AskUserQuestion": True}
    assert result["active_pane_width"] == 70
    assert result["preferred_backend"] == "tmux"
    assert result["should_focus_worker"] is True
    assert result["tui_enabled"] is True
    assert result["retention_days"] == 45
    assert settings_json_path.exists()
    assert load_active_pane_width(setting_toml_path) == 70
    assert load_preferred_backend(setting_toml_path) == "tmux"
    assert load_tui_enabled(setting_toml_path) is True
    assert load_retention_days(setting_toml_path) == 45


def test_init_with_tui_enabled_unset_writes_nothing(tmp_path: Path) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    result = init(
        InitDecisions(
            install_hooks=False,
            active_pane_width=None,
            preferred_backend=None,
            tui_enabled=None,
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    assert "tui_enabled" not in result
    assert not setting_toml_path.exists()


def test_init_writes_tui_enabled_independently_of_other_items(
    tmp_path: Path,
) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    result = init(
        InitDecisions(
            install_hooks=False,
            active_pane_width=None,
            preferred_backend=None,
            tui_enabled=True,
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    assert result["tui_enabled"] is True
    assert result["setting_toml_path"] == str(setting_toml_path)
    assert not settings_json_path.exists()
    assert load_tui_enabled(setting_toml_path) is True


def test_init_rejects_a_non_bool_should_focus_worker_before_writing_anything(
    tmp_path: Path,
) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    with pytest.raises(ValidationError):
        init(
            InitDecisions(
                install_hooks=True,
                active_pane_width=None,
                preferred_backend=None,
                should_focus_worker="yes",  # type: ignore[arg-type]
            ),
            settings_json_path=settings_json_path,
            setting_toml_path=setting_toml_path,
        )

    assert not settings_json_path.exists()
    assert not setting_toml_path.exists()


def test_init_with_retention_days_unset_writes_nothing(tmp_path: Path) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    result = init(
        InitDecisions(
            install_hooks=False,
            active_pane_width=None,
            preferred_backend=None,
            retention_days=None,
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    assert "retention_days" not in result
    assert not setting_toml_path.exists()


def test_init_writes_retention_days_independently_of_other_items(
    tmp_path: Path,
) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    result = init(
        InitDecisions(
            install_hooks=False,
            active_pane_width=None,
            preferred_backend=None,
            retention_days=45,
        ),
        settings_json_path=settings_json_path,
        setting_toml_path=setting_toml_path,
    )

    assert result["retention_days"] == 45
    assert result["setting_toml_path"] == str(setting_toml_path)
    assert not settings_json_path.exists()
    assert load_retention_days(setting_toml_path) == 45


def test_init_rejects_a_zero_retention_days_before_writing_anything(
    tmp_path: Path,
) -> None:
    settings_json_path = tmp_path / "settings.json"
    setting_toml_path = tmp_path / "setting.toml"

    with pytest.raises(ValidationError):
        init(
            InitDecisions(
                install_hooks=True,
                active_pane_width=None,
                preferred_backend=None,
                retention_days=0,
            ),
            settings_json_path=settings_json_path,
            setting_toml_path=setting_toml_path,
        )

    assert not settings_json_path.exists()
    assert not setting_toml_path.exists()


def test_check_retention_days_delegates_to_the_domain_validator() -> None:
    assert check_retention_days(45) == 45
    with pytest.raises(ValidationError):
        check_retention_days(0)


def _stub_build_gate(
    monkeypatch, *, npm_available: bool, tui_source_available: bool, bundle: bool
) -> None:
    monkeypatch.setattr(npm_runtime_module, "npm_available", lambda: npm_available)
    monkeypatch.setattr(
        npm_runtime_module, "tui_source_available", lambda: tui_source_available
    )
    monkeypatch.setattr(tui_bundle_module, "tui_bundle_available", lambda: bundle)


def test_should_run_tui_build_true_when_every_input_is_satisfied(monkeypatch) -> None:
    _stub_build_gate(
        monkeypatch, npm_available=True, tui_source_available=True, bundle=False
    )

    assert should_run_tui_build(tui_enabled_answer=True) is True


def test_should_run_tui_build_false_when_tui_enabled_answer_is_false(
    monkeypatch,
) -> None:
    _stub_build_gate(
        monkeypatch, npm_available=True, tui_source_available=True, bundle=False
    )

    assert should_run_tui_build(tui_enabled_answer=False) is False


def test_should_run_tui_build_false_when_npm_is_unavailable(monkeypatch) -> None:
    _stub_build_gate(
        monkeypatch, npm_available=False, tui_source_available=True, bundle=False
    )

    assert should_run_tui_build(tui_enabled_answer=True) is False


def test_should_run_tui_build_false_when_tui_source_is_unavailable(monkeypatch) -> None:
    _stub_build_gate(
        monkeypatch, npm_available=True, tui_source_available=False, bundle=False
    )

    assert should_run_tui_build(tui_enabled_answer=True) is False


def test_should_run_tui_build_false_when_a_usable_bundle_already_exists(
    monkeypatch,
) -> None:
    _stub_build_gate(
        monkeypatch, npm_available=True, tui_source_available=True, bundle=True
    )

    assert should_run_tui_build(tui_enabled_answer=True) is False


def test_attempt_tui_build_copies_builds_and_installs_the_bundle(
    monkeypatch, tmp_path: Path
) -> None:
    calls: dict[str, Path] = {}
    installed: dict[str, str | Path] = {}

    def fake_copy(dest: Path) -> None:
        calls["copy_dest"] = dest

    def fake_ci(source_dir: Path) -> None:
        calls["ci_source_dir"] = source_dir

    def fake_build(source_dir: Path) -> Path:
        calls["build_source_dir"] = source_dir
        built = source_dir / "dist" / "tree-tui.mjs"
        built.parent.mkdir(parents=True)
        built.write_text("built", encoding="utf-8")
        return built

    def fake_install(built_bundle_path: Path, *, fingerprint: str) -> None:
        installed["built_bundle_path"] = built_bundle_path
        installed["fingerprint"] = fingerprint

    monkeypatch.setattr(npm_runtime_module, "copy_tui_source", fake_copy)
    monkeypatch.setattr(npm_runtime_module, "run_npm_ci", fake_ci)
    monkeypatch.setattr(npm_runtime_module, "run_npm_build", fake_build)
    monkeypatch.setattr(
        npm_runtime_module, "embedded_tui_source_fingerprint", lambda: "fingerprint-1"
    )
    monkeypatch.setattr(tui_launcher_module, "install_bundle_from_build", fake_install)

    attempt_tui_build()

    assert calls["copy_dest"] == calls["ci_source_dir"] == calls["build_source_dir"]
    assert (
        installed["built_bundle_path"]
        == calls["build_source_dir"] / "dist" / "tree-tui.mjs"
    )
    assert installed["fingerprint"] == "fingerprint-1"


def test_attempt_tui_build_raises_when_fingerprint_disappears_mid_build(
    monkeypatch, tmp_path: Path
) -> None:
    """TOCTOU guard: `should_run_tui_build()`'s earlier `tui_source_available()`
    check does not guarantee the source is still there by the time the build
    finishes -- if the fingerprint comes back `None`, this must raise instead
    of installing a bundle with no recorded provenance."""

    def fake_build(source_dir: Path) -> Path:
        built = source_dir / "dist" / "tree-tui.mjs"
        built.parent.mkdir(parents=True)
        built.write_text("built", encoding="utf-8")
        return built

    monkeypatch.setattr(npm_runtime_module, "copy_tui_source", lambda dest: None)
    monkeypatch.setattr(npm_runtime_module, "run_npm_ci", lambda source_dir: None)
    monkeypatch.setattr(npm_runtime_module, "run_npm_build", fake_build)
    monkeypatch.setattr(
        npm_runtime_module, "embedded_tui_source_fingerprint", lambda: None
    )

    def fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("install_bundle_from_build must not be called")

    monkeypatch.setattr(
        tui_launcher_module, "install_bundle_from_build", fail_if_called
    )

    with pytest.raises(BackendError):
        attempt_tui_build()

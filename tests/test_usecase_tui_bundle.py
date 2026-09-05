import sukuna.infrastructure.npm_runtime as npm_runtime_module
import sukuna.infrastructure.tui.launcher as tui_launcher_module
from sukuna.usecase.tui_bundle import (
    state_dir_bundle_usable,
    tui_bundle_available,
    tui_bundle_source,
)


def _stub_package_bundle(monkeypatch, *, present: bool) -> None:
    monkeypatch.setattr(tui_launcher_module, "bundle_present", lambda: present)


def _stub_state_dir_bundle(
    monkeypatch, *, exists: bool, stamp: str | None, current: str | None
) -> None:
    monkeypatch.setattr(tui_launcher_module, "state_dir_bundle_exists", lambda: exists)
    monkeypatch.setattr(
        tui_launcher_module, "state_dir_stamp_fingerprint", lambda: stamp
    )
    monkeypatch.setattr(
        npm_runtime_module, "embedded_tui_source_fingerprint", lambda: current
    )


def test_state_dir_bundle_usable_true_when_stamp_matches(monkeypatch) -> None:
    _stub_state_dir_bundle(monkeypatch, exists=True, stamp="abc", current="abc")

    assert state_dir_bundle_usable() is True


def test_state_dir_bundle_usable_false_when_stamp_mismatches(monkeypatch) -> None:
    _stub_state_dir_bundle(monkeypatch, exists=True, stamp="abc", current="def")

    assert state_dir_bundle_usable() is False


def test_state_dir_bundle_usable_false_when_bundle_file_missing(monkeypatch) -> None:
    _stub_state_dir_bundle(monkeypatch, exists=False, stamp="abc", current="abc")

    assert state_dir_bundle_usable() is False


def test_tui_bundle_source_prefers_package_when_present(monkeypatch) -> None:
    _stub_package_bundle(monkeypatch, present=True)
    _stub_state_dir_bundle(monkeypatch, exists=True, stamp="abc", current="abc")

    assert tui_bundle_source() == "package"


def test_tui_bundle_source_falls_back_to_state_dir(monkeypatch) -> None:
    _stub_package_bundle(monkeypatch, present=False)
    _stub_state_dir_bundle(monkeypatch, exists=True, stamp="abc", current="abc")

    assert tui_bundle_source() == "state_dir"


def test_tui_bundle_source_none_when_neither_usable(monkeypatch) -> None:
    _stub_package_bundle(monkeypatch, present=False)
    _stub_state_dir_bundle(monkeypatch, exists=False, stamp=None, current=None)

    assert tui_bundle_source() is None


def test_tui_bundle_source_none_on_version_skew(monkeypatch) -> None:
    """OP6: a state-dir bundle whose stamp no longer matches the currently
    embedded source is treated as absent, not present-but-stale."""
    _stub_package_bundle(monkeypatch, present=False)
    _stub_state_dir_bundle(monkeypatch, exists=True, stamp="old", current="new")

    assert tui_bundle_source() is None
    assert tui_bundle_available() is False


def test_tui_bundle_available_matches_source_being_non_none(monkeypatch) -> None:
    _stub_package_bundle(monkeypatch, present=True)
    _stub_state_dir_bundle(monkeypatch, exists=False, stamp=None, current=None)

    assert tui_bundle_available() is True

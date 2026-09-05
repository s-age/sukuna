import sukuna.usecase.tui_bundle as tui_bundle_module
from sukuna.usecase.doctor import doctor


def test_doctor_returns_exact_key_set() -> None:
    result = doctor()

    assert set(result.keys()) == {
        "claude_cli",
        "it2run",
        "it2run_available",
        "tmux",
        "tmux_available",
        "platform",
        "node_tui_available",
        "tui_bundle_source",
        "tui_bundle_present",
    }
    assert isinstance(result["it2run"], str)
    assert isinstance(result["tmux"], str)
    assert isinstance(result["it2run_available"], bool)
    assert isinstance(result["tmux_available"], bool)
    assert isinstance(result["node_tui_available"], bool)
    assert result["tui_bundle_source"] is None or isinstance(
        result["tui_bundle_source"], str
    )
    assert isinstance(result["tui_bundle_present"], bool)


def test_doctor_reports_package_bundle_source(monkeypatch) -> None:
    monkeypatch.setattr(tui_bundle_module, "tui_bundle_source", lambda: "package")

    result = doctor()

    assert result["tui_bundle_source"] == "package"
    assert result["tui_bundle_present"] is True


def test_doctor_reports_state_dir_bundle_source(monkeypatch) -> None:
    monkeypatch.setattr(tui_bundle_module, "tui_bundle_source", lambda: "state_dir")

    result = doctor()

    assert result["tui_bundle_source"] == "state_dir"
    assert result["tui_bundle_present"] is True


def test_doctor_reports_no_bundle_source(monkeypatch) -> None:
    monkeypatch.setattr(tui_bundle_module, "tui_bundle_source", lambda: None)

    result = doctor()

    assert result["tui_bundle_source"] is None
    assert result["tui_bundle_present"] is False

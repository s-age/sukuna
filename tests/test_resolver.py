import shutil
from pathlib import Path

import pytest

import sukuna.infrastructure.settings as settings_module
from sukuna.errors import ValidationError
from sukuna.infrastructure.terminal import resolver


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)


def test_detect_backend_returns_tmux_when_only_tmux_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")

    assert resolver.detect_backend() == "tmux"


def test_detect_backend_returns_iterm2_when_only_iterm_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("ITERM_SESSION_ID", "w0t0p0:some-uuid")

    assert resolver.detect_backend() == "iterm2"


def test_detect_backend_raises_when_neither_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)

    with pytest.raises(ValidationError):
        resolver.detect_backend()


def test_detect_backend_prefers_tmux_when_both_set_and_no_preference_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
    monkeypatch.setenv("ITERM_SESSION_ID", "w0t0p0:some-uuid")
    monkeypatch.setattr(settings_module, "load_preferred_backend", lambda: None)

    assert resolver.detect_backend() == "tmux"


def test_detect_backend_prefers_tmux_when_both_set_and_preference_is_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
    monkeypatch.setenv("ITERM_SESSION_ID", "w0t0p0:some-uuid")
    monkeypatch.setattr(settings_module, "load_preferred_backend", lambda: "tmux")

    assert resolver.detect_backend() == "tmux"


def test_detect_backend_ties_break_to_iterm2_when_both_set_and_preference_is_iterm2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
    monkeypatch.setenv("ITERM_SESSION_ID", "w0t0p0:some-uuid")
    monkeypatch.setattr(settings_module, "load_preferred_backend", lambda: "iterm2")

    assert resolver.detect_backend() == "iterm2"


def test_tmux_installed_true_when_shutil_which_finds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/tmux" if name == "tmux" else None,
    )

    assert resolver.tmux_installed() is True


def test_tmux_installed_false_when_shutil_which_does_not_find_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert resolver.tmux_installed() is False


def test_iterm2_installed_true_when_session_id_is_currently_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ITERM_SESSION_ID", "w0t0p0:some-uuid")
    monkeypatch.setattr(
        resolver, "_ITERM_BUNDLE_PATHS", (tmp_path / "does-not-exist.app",)
    )

    assert resolver.iterm2_installed() is True


def test_iterm2_installed_true_when_a_bundle_path_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    bundle_path = tmp_path / "iTerm.app"
    bundle_path.mkdir()
    monkeypatch.setattr(
        resolver, "_ITERM_BUNDLE_PATHS", (tmp_path / "does-not-exist.app", bundle_path)
    )

    assert resolver.iterm2_installed() is True


def test_iterm2_installed_false_when_neither_session_nor_bundle_path_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    monkeypatch.setattr(
        resolver, "_ITERM_BUNDLE_PATHS", (tmp_path / "does-not-exist.app",)
    )

    assert resolver.iterm2_installed() is False


def test_default_it2run_path_derives_from_the_first_existing_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first_bundle = tmp_path / "first" / "iTerm.app"
    first_bundle.mkdir(parents=True)
    second_bundle = tmp_path / "second" / "iTerm.app"
    second_bundle.mkdir(parents=True)
    monkeypatch.setattr(resolver, "_ITERM_BUNDLE_PATHS", (first_bundle, second_bundle))

    assert (
        resolver.default_it2run_path()
        == first_bundle / "Contents" / "Resources" / "it2run"
    )


def test_default_it2run_path_falls_back_to_a_later_bundle_when_earlier_ones_are_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing_bundle = tmp_path / "does-not-exist.app"
    home_bundle = tmp_path / "home" / "iTerm.app"
    home_bundle.mkdir(parents=True)
    monkeypatch.setattr(resolver, "_ITERM_BUNDLE_PATHS", (missing_bundle, home_bundle))

    assert (
        resolver.default_it2run_path()
        == home_bundle / "Contents" / "Resources" / "it2run"
    )


def test_default_it2run_path_falls_back_to_the_first_candidate_when_none_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first_bundle = tmp_path / "first" / "iTerm.app"
    second_bundle = tmp_path / "second" / "iTerm.app"
    monkeypatch.setattr(resolver, "_ITERM_BUNDLE_PATHS", (first_bundle, second_bundle))

    assert (
        resolver.default_it2run_path()
        == first_bundle / "Contents" / "Resources" / "it2run"
    )


def test_infer_backend_returns_tmux_for_percent_prefixed_pane_ref() -> None:
    assert resolver.infer_backend("%12") == "tmux"


def test_infer_backend_returns_iterm2_for_uuid_pane_ref() -> None:
    assert resolver.infer_backend("ABCDEF12-3456-7890-ABCD-EF1234567890") == "iterm2"


def test_orchestrator_pane_ref_tmux_returns_tmux_pane_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX_PANE", "%3")

    assert resolver.orchestrator_pane_ref("tmux") == "%3"


def test_orchestrator_pane_ref_tmux_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TMUX_PANE", raising=False)

    assert resolver.orchestrator_pane_ref("tmux") is None


def test_orchestrator_pane_ref_tmux_returns_none_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX_PANE", "")

    assert resolver.orchestrator_pane_ref("tmux") is None


def test_orchestrator_pane_ref_iterm2_extracts_uuid_after_colon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ITERM_SESSION_ID", "w0t0p0:some-uuid")

    assert resolver.orchestrator_pane_ref("iterm2") == "some-uuid"


def test_orchestrator_pane_ref_iterm2_returns_raw_when_no_colon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ITERM_SESSION_ID", "some-uuid-without-colon")

    assert resolver.orchestrator_pane_ref("iterm2") == "some-uuid-without-colon"


def test_orchestrator_pane_ref_iterm2_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)

    assert resolver.orchestrator_pane_ref("iterm2") is None


def test_orchestrator_pane_ref_iterm2_returns_none_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ITERM_SESSION_ID", "")

    assert resolver.orchestrator_pane_ref("iterm2") is None


def test_orchestrator_pane_ref_iterm2_returns_none_when_uuid_part_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ITERM_SESSION_ID", "w0t0p0:")

    assert resolver.orchestrator_pane_ref("iterm2") is None


@pytest.mark.parametrize(
    "shell_path", ["/bin/zsh", "/usr/bin/bash", "/bin/dash", "/bin/sh"]
)
def test_resolve_login_shell_trusts_shell_env_when_known_safe(
    monkeypatch: pytest.MonkeyPatch, shell_path: str
) -> None:
    monkeypatch.setenv("SHELL", shell_path)

    assert resolver.resolve_login_shell() == shell_path


@pytest.mark.parametrize("shell_path", ["/usr/local/bin/fish", "/bin/tcsh", "/bin/csh"])
def test_resolve_login_shell_falls_back_when_shell_env_is_not_known_safe(
    monkeypatch: pytest.MonkeyPatch, shell_path: str
) -> None:
    monkeypatch.setenv("SHELL", shell_path)
    monkeypatch.setattr(
        shutil, "which", lambda name: "/opt/homebrew/bin/zsh" if name == "zsh" else None
    )

    assert resolver.resolve_login_shell() == "/opt/homebrew/bin/zsh"


def test_resolve_login_shell_falls_back_when_shell_env_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(
        shutil, "which", lambda name: "/opt/homebrew/bin/zsh" if name == "zsh" else None
    )

    assert resolver.resolve_login_shell() == "/opt/homebrew/bin/zsh"


def test_resolve_login_shell_falls_back_when_shell_env_is_set_but_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "")
    monkeypatch.setattr(
        shutil, "which", lambda name: "/opt/homebrew/bin/zsh" if name == "zsh" else None
    )

    assert resolver.resolve_login_shell() == "/opt/homebrew/bin/zsh"


def test_resolve_login_shell_fallback_chain_prefers_zsh_over_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: {"zsh": "/usr/bin/zsh", "bash": "/usr/bin/bash"}.get(name),
    )

    assert resolver.resolve_login_shell() == "/usr/bin/zsh"


def test_resolve_login_shell_fallback_chain_uses_bash_when_zsh_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/bash" if name == "bash" else None
    )

    assert resolver.resolve_login_shell() == "/usr/bin/bash"


def test_resolve_login_shell_fallback_chain_uses_bin_sh_as_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert resolver.resolve_login_shell() == "/bin/sh"

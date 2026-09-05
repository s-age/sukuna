from pathlib import Path
from typing import Any

import pytest
from _terminal_fakes import make_worker as _make_worker

import sukuna.infrastructure.claude_projects as claude_projects_module
import sukuna.infrastructure.npm_runtime as npm_runtime_module
import sukuna.infrastructure.settings as settings_module
import sukuna.infrastructure.tui.launcher as tui_launcher_module
from sukuna.errors import ValidationError
from sukuna.infrastructure.registry import Registry
from sukuna.usecase.tree import launch_tree_tui, tree, tree_payload


def _pin_state_dir_bundle_usable_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """`launch_tree_tui()` now resolves `state_dir_bundle_usable()` off real
    disk state (`sukuna_state_dir()`), which is only incidentally `False` on
    a machine that has never run `sukuna-cli init`'s TUI build -- pin it
    explicitly so these tests don't start depending on that."""
    monkeypatch.setattr(tui_launcher_module, "state_dir_bundle_exists", lambda: False)
    monkeypatch.setattr(
        npm_runtime_module, "embedded_tui_source_fingerprint", lambda: None
    )


def make_worker(repo: Path, *, parent_session_id, **overrides):
    return _make_worker(repo, parent_session_id=parent_session_id, **overrides)


def test_tree_payload_matches_the_groups_and_hidden_parent_groups_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    worker.session_log_path = f"/logs/{worker.name}.jsonl"
    registry.add(worker)
    monkeypatch.setattr(
        claude_projects_module,
        "resolve_parent_session_log_path",
        lambda parent_session_id: f"/logs/{parent_session_id}.jsonl",
    )

    payload = tree_payload(registry)

    assert set(payload.keys()) == {
        "groups",
        "hidden_parent_groups",
        "group_session_log_paths",
    }
    assert "orchestrator-1" in payload["groups"]
    assert payload["groups"]["orchestrator-1"][0]["name"] == worker.name
    assert payload["groups"]["orchestrator-1"][0]["session_log_path"] == (
        f"/logs/{worker.name}.jsonl"
    )
    assert isinstance(payload["hidden_parent_groups"], int)
    assert payload["group_session_log_paths"] == {
        "orchestrator-1": "/logs/orchestrator-1.jsonl"
    }


def test_tree_payload_never_touches_the_filesystem_for_per_node_session_log_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Card 722DF17A: `tree_payload()` must read `session_log_path` off
    each `WorkerRecord` (cached by the registry's own mutate()/replace()
    hook at state-transition time), never rescan the filesystem itself --
    unlike `resolve_group_session_log_paths()` (out of scope, still
    filesystem-backed), asserted here by monkeypatching
    `resolve_session_log_path` to something that would fail the test loudly
    if it were ever called."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    kept = make_worker(tmp_path, parent_session_id="orchestrator-kept", suffix="kept")
    kept.updated_at = "2026-08-29T02:00:00+00:00"
    dropped = make_worker(
        tmp_path, parent_session_id="orchestrator-dropped", suffix="dropped"
    )
    dropped.updated_at = "2026-08-29T01:00:00+00:00"
    registry.add(kept)
    registry.add(dropped)

    def fail_if_called(worktree: str, name: str) -> str | None:
        raise AssertionError(
            "tree_payload() must not call the filesystem resolver for "
            "per-node session_log_path resolution"
        )

    monkeypatch.setattr(
        claude_projects_module, "resolve_session_log_path", fail_if_called
    )
    monkeypatch.setattr(
        claude_projects_module,
        "resolve_parent_session_log_path",
        lambda parent_session_id: None,
    )

    payload = tree_payload(registry, limit=1)

    assert payload["groups"]["orchestrator-kept"][0]["session_log_path"] is None


def test_tree_payload_reflects_the_registrys_own_session_log_path_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    worker.session_log_path = "/logs/cached-from-registry.jsonl"
    registry.add(worker)
    monkeypatch.setattr(
        claude_projects_module,
        "resolve_parent_session_log_path",
        lambda parent_session_id: None,
    )

    payload = tree_payload(registry)

    assert (
        payload["groups"]["orchestrator-1"][0]["session_log_path"]
        == "/logs/cached-from-registry.jsonl"
    )


def test_tree_payload_attaches_resumable_true_for_a_worker_at_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    registry.add(worker)
    monkeypatch.setattr(
        claude_projects_module,
        "resolve_parent_session_log_path",
        lambda parent_session_id: None,
    )

    payload = tree_payload(registry, cwd=str(tmp_path))

    assert payload["groups"]["orchestrator-1"][0]["resumable"] is True


def test_tree_payload_attaches_resumable_false_for_a_worker_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    registry.add(worker)
    monkeypatch.setattr(
        claude_projects_module,
        "resolve_parent_session_log_path",
        lambda parent_session_id: None,
    )

    payload = tree_payload(registry, cwd=str(tmp_path / "elsewhere"))

    assert payload["groups"]["orchestrator-1"][0]["resumable"] is False


def test_tree_payload_defaults_resumable_to_false_when_cwd_is_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")
    registry.add(worker)
    monkeypatch.setattr(
        claude_projects_module,
        "resolve_parent_session_log_path",
        lambda parent_session_id: None,
    )

    payload = tree_payload(registry)

    assert payload["groups"]["orchestrator-1"][0]["resumable"] is False


def test_tree_payload_resumable_only_drops_the_non_resumable_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    resumable = make_worker(
        tmp_path, parent_session_id="orchestrator-resumable", suffix="resumable"
    )
    stale = make_worker(
        tmp_path,
        parent_session_id="orchestrator-stale",
        suffix="stale",
        worktree=tmp_path / "elsewhere",
    )
    registry.add(resumable)
    registry.add(stale)
    monkeypatch.setattr(
        claude_projects_module,
        "resolve_parent_session_log_path",
        lambda parent_session_id: None,
    )

    payload = tree_payload(registry, cwd=str(tmp_path), resumable_only=True)

    assert set(payload["groups"].keys()) == {"orchestrator-resumable"}
    assert "orchestrator-stale" not in payload["group_session_log_paths"]


def test_tree_tags_a_resumable_worker_in_text_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    resumable = make_worker(
        tmp_path, parent_session_id="orchestrator-1", suffix="resumable"
    )
    stale = make_worker(
        tmp_path,
        parent_session_id="orchestrator-1",
        suffix="stale",
        worktree=tmp_path / "elsewhere",
    )
    registry.add(resumable)
    registry.add(stale)

    out = tree(registry, cwd=str(tmp_path))

    assert f"{resumable.name} " in out
    resumable_line = next(line for line in out.splitlines() if resumable.name in line)
    stale_line = next(line for line in out.splitlines() if stale.name in line)
    assert "[resumable]" in resumable_line
    assert "[resumable]" not in stale_line


def test_tree_resumable_only_prunes_text_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    resumable = make_worker(
        tmp_path, parent_session_id="orchestrator-resumable", suffix="resumable"
    )
    stale = make_worker(
        tmp_path,
        parent_session_id="orchestrator-stale",
        suffix="stale",
        worktree=tmp_path / "elsewhere",
    )
    registry.add(resumable)
    registry.add(stale)

    out = tree(registry, cwd=str(tmp_path), resumable_only=True)

    assert resumable.name in out
    assert stale.name not in out
    assert "orchestrator-stale" not in out


def _contains_a_string_marker(items) -> bool:
    for item in items:
        if isinstance(item, str):
            return True
        if _contains_a_string_marker(item["children"]):
            return True
    return False


def test_tree_payload_preserves_cycle_marker_strings_as_plain_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    worker_b = make_worker(tmp_path, parent_session_id=None, suffix="b")
    worker_a = make_worker(
        tmp_path, parent_session_id=None, suffix="a", parent_worker_name=worker_b.name
    )
    worker_b.parent_worker_name = worker_a.name
    registry.add(worker_a)
    registry.add(worker_b)
    monkeypatch.setattr(
        claude_projects_module,
        "resolve_parent_session_log_path",
        lambda parent_session_id: None,
    )

    payload = tree_payload(registry)

    cycle_group = payload["groups"]["(cycle: unreachable from any root)"]
    assert _contains_a_string_marker(cycle_group)
    assert (
        payload["group_session_log_paths"]["(cycle: unreachable from any root)"] is None
    )


def test_launch_tree_tui_raises_when_tui_enabled_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setattr(settings_module, "load_tui_enabled", lambda: False)

    with pytest.raises(ValidationError):
        launch_tree_tui(registry)


def test_launch_tree_tui_raises_when_tui_enabled_is_explicitly_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setattr(settings_module, "load_tui_enabled", lambda: False)

    with pytest.raises(ValidationError):
        launch_tree_tui(registry)


def test_launch_tree_tui_calls_the_launcher_with_the_selected_payload_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With only one group
    (<= the default limit of 5), `tree_payload(registry)` (limit omitted,
    defaults to 5) and `tree_payload(registry, limit=None)` would coincide
    by accident, so this fixture uses 6 groups -- more than the default
    limit -- so a `limit=5` call would actually drop one, letting the
    assertions below tell the two apart."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    for index in range(6):
        worker = make_worker(
            tmp_path, parent_session_id=f"orchestrator-{index}", suffix=f"a{index}"
        )
        registry.add(worker)
    monkeypatch.setattr(settings_module, "load_tui_enabled", lambda: True)
    monkeypatch.setattr(
        claude_projects_module,
        "resolve_parent_session_log_path",
        lambda parent_session_id: None,
    )
    _pin_state_dir_bundle_usable_to_false(monkeypatch)
    captured: dict[str, object] = {}

    def fake_launch(
        payload: dict[str, object], *, state_dir_bundle_usable: bool
    ) -> int:
        captured["payload"] = payload
        return 0

    monkeypatch.setattr(tui_launcher_module, "launch_tree_tui", fake_launch)

    exit_code = launch_tree_tui(registry)

    assert exit_code == 0
    unlimited_payload = tree_payload(registry, limit=None)
    limited_payload = tree_payload(registry, limit=5)
    assert unlimited_payload["hidden_parent_groups"] == 0
    assert limited_payload["hidden_parent_groups"] == 1
    assert captured["payload"] == unlimited_payload
    assert captured["payload"] != limited_payload


def test_launch_tree_tui_payload_carries_the_resumable_field_per_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    resumable = make_worker(
        tmp_path, parent_session_id="orchestrator-1", suffix="resumable"
    )
    stale = make_worker(
        tmp_path,
        parent_session_id="orchestrator-1",
        suffix="stale",
        worktree=tmp_path / "elsewhere",
    )
    registry.add(resumable)
    registry.add(stale)
    monkeypatch.setattr(settings_module, "load_tui_enabled", lambda: True)
    monkeypatch.setattr(
        claude_projects_module,
        "resolve_parent_session_log_path",
        lambda parent_session_id: None,
    )
    _pin_state_dir_bundle_usable_to_false(monkeypatch)
    captured: dict[str, Any] = {}

    def fake_launch(payload: dict[str, Any], *, state_dir_bundle_usable: bool) -> int:
        captured["payload"] = payload
        return 0

    monkeypatch.setattr(tui_launcher_module, "launch_tree_tui", fake_launch)

    launch_tree_tui(registry, cwd=str(tmp_path))

    nodes = captured["payload"]["groups"]["orchestrator-1"]
    resumable_flags = {node["name"]: node["resumable"] for node in nodes}
    assert resumable_flags == {resumable.name: True, stale.name: False}


def test_launch_tree_tui_returns_the_launchers_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setattr(settings_module, "load_tui_enabled", lambda: True)
    _pin_state_dir_bundle_usable_to_false(monkeypatch)
    monkeypatch.setattr(
        tui_launcher_module,
        "launch_tree_tui",
        lambda payload, *, state_dir_bundle_usable: 7,
    )

    assert launch_tree_tui(registry) == 7

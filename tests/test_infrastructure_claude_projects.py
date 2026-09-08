import os
from pathlib import Path

import pytest

import sukuna.infrastructure.claude_projects as claude_projects_module
from sukuna.infrastructure.claude_projects import (
    resolve_last_session_state,
    resolve_parent_session_log_path,
    resolve_session_log_path,
)


def _point_projects_dir_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(claude_projects_module, "default_projects_dir", lambda: path)


def test_resolve_session_log_path_returns_none_when_project_dir_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_projects_dir_at(monkeypatch, tmp_path / "projects")

    assert resolve_session_log_path("/some/worktree", "ccw-x-1") is None


def test_resolve_session_log_path_finds_the_matching_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_dir = tmp_path / "projects"
    worktree = "/Users/example/repo"
    encoded = "-Users-example-repo"
    project_dir = projects_dir / encoded
    project_dir.mkdir(parents=True)
    (project_dir / "other-session.jsonl").write_text(
        '{"type":"custom-title","customTitle":"ccw-x-other","sessionId":"other-session"}\n'
    )
    target = project_dir / "target-session.jsonl"
    target.write_text(
        '{"type":"custom-title","customTitle":"ccw-x-1","sessionId":"target-session"}\n'
        '{"type":"agent-name"}\n'
    )
    _point_projects_dir_at(monkeypatch, projects_dir)

    assert resolve_session_log_path(worktree, "ccw-x-1") == str(target)


def test_resolve_session_log_path_returns_none_when_no_jsonl_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_dir = tmp_path / "projects"
    worktree = "/Users/example/repo"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    (project_dir / "other-session.jsonl").write_text(
        '{"type":"custom-title","customTitle":"ccw-x-other","sessionId":"other-session"}\n'
    )
    _point_projects_dir_at(monkeypatch, projects_dir)

    assert resolve_session_log_path(worktree, "ccw-x-1") is None


def test_resolve_session_log_path_prefers_the_newest_match_over_respawn_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`respawn` reuses the same `WorkerRecord.name`, so `claude -n <name>`
    runs again and leaves a second jsonl with the same `custom-title`
    behind. Filenames are session UUIDs, so this uses filenames that sort
    the *stale* one after the *live* one lexically to prove the pick is
    mtime-based, not glob/sort order."""
    projects_dir = tmp_path / "projects"
    worktree = "/Users/example/repo"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    custom_title = '{"type":"custom-title","customTitle":"ccw-x-1","sessionId":"s"}\n'
    stale = project_dir / "a-stale-session.jsonl"
    stale.write_text(custom_title)
    live = project_dir / "z-live-session.jsonl"
    live.write_text(custom_title)
    now = 2_000_000_000.0
    os.utime(stale, (now - 3600, now - 3600))
    os.utime(live, (now, now))
    _point_projects_dir_at(monkeypatch, projects_dir)

    assert resolve_session_log_path(worktree, "ccw-x-1") == str(live)


def test_resolve_session_log_path_skips_a_candidate_that_raises_oserror_while_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read failure on one candidate jsonl (permission error, the file
    vanishing mid-scan -- TOCTOU between `glob()` and `open()`) must not
    sink the whole worker's resolution if another candidate in the same
    directory is still readable and matches."""
    projects_dir = tmp_path / "projects"
    worktree = "/Users/example/repo"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    custom_title = '{"type":"custom-title","customTitle":"ccw-x-1","sessionId":"s"}\n'
    broken = project_dir / "broken-session.jsonl"
    broken.write_text(custom_title)
    healthy = project_dir / "healthy-session.jsonl"
    healthy.write_text(custom_title)
    _point_projects_dir_at(monkeypatch, projects_dir)
    original_read = claude_projects_module._read_leading_entries

    def flaky_read(path: Path, count: int):
        if path == broken:
            raise OSError("permission denied")
        return original_read(path, count)

    monkeypatch.setattr(claude_projects_module, "_read_leading_entries", flaky_read)

    assert resolve_session_log_path(worktree, "ccw-x-1") == str(healthy)


def test_resolve_session_log_path_returns_none_when_every_candidate_fails_to_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-worker-null-degrade contract as the worst case of the same
    per-candidate try/except: everything failing degrades to `None`
    (an exit-2 `sukuna-cli tree --tui` run) rather than propagating an
    `OSError` up through `tree_payload()` and sinking every other worker's
    resolution in the same call."""
    projects_dir = tmp_path / "projects"
    worktree = "/Users/example/repo"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    (project_dir / "target-session.jsonl").write_text(
        '{"type":"custom-title","customTitle":"ccw-x-1","sessionId":"s"}\n'
    )
    _point_projects_dir_at(monkeypatch, projects_dir)

    def always_fails(path: Path, count: int):
        raise OSError("permission denied")

    monkeypatch.setattr(claude_projects_module, "_read_leading_entries", always_fails)

    assert resolve_session_log_path(worktree, "ccw-x-1") is None


def test_resolve_session_log_path_respects_the_scan_lines_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_dir = tmp_path / "projects"
    worktree = "/Users/example/repo"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    filler = "".join('{"type":"mode"}\n' for _ in range(5))
    (project_dir / "target-session.jsonl").write_text(
        filler
        + '{"type":"custom-title","customTitle":"ccw-x-1","sessionId":"target-session"}\n'
    )
    _point_projects_dir_at(monkeypatch, projects_dir)

    assert resolve_session_log_path(worktree, "ccw-x-1", scan_lines=3) is None
    assert resolve_session_log_path(worktree, "ccw-x-1", scan_lines=10) is not None


def test_resolve_session_log_path_rejects_a_candidate_older_than_not_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Respawn stuck-match regression: a stale pre-reset jsonl
    left behind by an earlier `claude -n <name>` run must not be handed
    back once `session_log_reset_at` has moved past its mtime -- otherwise
    the domain hook's `session_log_path is not None` guard latches onto it
    forever."""
    projects_dir = tmp_path / "projects"
    worktree = "/Users/example/repo"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    stale = project_dir / "stale-session.jsonl"
    stale.write_text(
        '{"type":"custom-title","customTitle":"ccw-x-1","sessionId":"s"}\n'
    )
    stale_mtime = 2_000_000_000.0
    os.utime(stale, (stale_mtime, stale_mtime))
    _point_projects_dir_at(monkeypatch, projects_dir)
    reset_at = "2033-05-18T04:33:20+00:00"  # one hour after stale_mtime, in UTC

    assert resolve_session_log_path(worktree, "ccw-x-1", reset_at) is None


def test_resolve_session_log_path_accepts_a_candidate_no_older_than_not_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the regression above: once a fresh session file
    appears with an mtime at or after `session_log_reset_at`, it resolves
    normally."""
    projects_dir = tmp_path / "projects"
    worktree = "/Users/example/repo"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    fresh = project_dir / "fresh-session.jsonl"
    fresh.write_text(
        '{"type":"custom-title","customTitle":"ccw-x-1","sessionId":"s"}\n'
    )
    fresh_mtime = 2_000_003_600.0
    os.utime(fresh, (fresh_mtime, fresh_mtime))
    _point_projects_dir_at(monkeypatch, projects_dir)
    reset_at = "2033-05-18T04:33:20+00:00"  # exactly fresh_mtime, in UTC

    assert resolve_session_log_path(worktree, "ccw-x-1", reset_at) == str(fresh)


def test_resolve_session_log_path_with_no_not_before_behaves_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_dir = tmp_path / "projects"
    worktree = "/Users/example/repo"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    target = project_dir / "target-session.jsonl"
    target.write_text(
        '{"type":"custom-title","customTitle":"ccw-x-1","sessionId":"s"}\n'
    )
    _point_projects_dir_at(monkeypatch, projects_dir)

    assert resolve_session_log_path(worktree, "ccw-x-1", None) == str(target)


def test_resolve_parent_session_log_path_returns_none_when_projects_dir_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_projects_dir_at(monkeypatch, tmp_path / "projects")

    assert resolve_parent_session_log_path("session-1") is None


def test_resolve_parent_session_log_path_finds_the_file_named_after_the_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parent session id *is* the jsonl's filename -- no custom-title
    correlation, no `worktree` narrowing (parent sessions have no recorded
    worktree), just a glob across every worktree's project directory."""
    projects_dir = tmp_path / "projects"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    target = project_dir / "session-1.jsonl"
    target.write_text('{"type":"summary"}\n')
    (project_dir / "session-2.jsonl").write_text('{"type":"summary"}\n')
    _point_projects_dir_at(monkeypatch, projects_dir)

    assert resolve_parent_session_log_path("session-1") == str(target)


def test_resolve_parent_session_log_path_returns_none_when_no_file_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_dir = tmp_path / "projects"
    project_dir = projects_dir / "-Users-example-repo"
    project_dir.mkdir(parents=True)
    (project_dir / "other-session.jsonl").write_text('{"type":"summary"}\n')
    _point_projects_dir_at(monkeypatch, projects_dir)

    assert resolve_parent_session_log_path("session-1") is None


def test_resolve_parent_session_log_path_searches_across_every_worktree_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_dir = tmp_path / "projects"
    other_dir = projects_dir / "-Users-example-other-repo"
    other_dir.mkdir(parents=True)
    target_dir = projects_dir / "-Users-example-target-repo"
    target_dir.mkdir(parents=True)
    target = target_dir / "session-1.jsonl"
    target.write_text('{"type":"summary"}\n')
    _point_projects_dir_at(monkeypatch, projects_dir)

    assert resolve_parent_session_log_path("session-1") == str(target)


def test_resolve_parent_session_log_path_prefers_the_newest_match_when_more_than_one_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session UUIDs are globally unique, so this should not happen in
    practice -- but the tie-break is exercised for consistency with
    `resolve_session_log_path()`."""
    projects_dir = tmp_path / "projects"
    dir_a = projects_dir / "-Users-example-repo-a"
    dir_a.mkdir(parents=True)
    dir_b = projects_dir / "-Users-example-repo-b"
    dir_b.mkdir(parents=True)
    stale = dir_a / "session-1.jsonl"
    stale.write_text('{"type":"summary"}\n')
    live = dir_b / "session-1.jsonl"
    live.write_text('{"type":"summary"}\n')
    now = 2_000_000_000.0
    os.utime(stale, (now - 3600, now - 3600))
    os.utime(live, (now, now))
    _point_projects_dir_at(monkeypatch, projects_dir)

    assert resolve_parent_session_log_path("session-1") == str(live)


def test_resolve_parent_session_log_path_skips_a_candidate_that_raises_oserror_on_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_dir = tmp_path / "projects"
    dir_a = projects_dir / "-Users-example-repo-a"
    dir_a.mkdir(parents=True)
    dir_b = projects_dir / "-Users-example-repo-b"
    dir_b.mkdir(parents=True)
    broken = dir_a / "session-1.jsonl"
    broken.write_text('{"type":"summary"}\n')
    healthy = dir_b / "session-1.jsonl"
    healthy.write_text('{"type":"summary"}\n')
    _point_projects_dir_at(monkeypatch, projects_dir)
    original_stat = Path.stat

    def flaky_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == broken:
            raise OSError("permission denied")
        return original_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    assert resolve_parent_session_log_path("session-1") == str(healthy)


def test_resolve_last_session_state_returns_none_none_for_a_none_path() -> None:
    assert resolve_last_session_state(None) == (None, None)


def test_resolve_last_session_state_returns_none_none_for_a_missing_file(
    tmp_path: Path,
) -> None:
    assert resolve_last_session_state(str(tmp_path / "missing.jsonl")) == (None, None)


def test_resolve_last_session_state_extracts_the_last_model_and_permission_mode(
    tmp_path: Path,
) -> None:
    session_log = tmp_path / "session.jsonl"
    session_log.write_text(
        '{"message":{"model":"claude-opus-5"}}\n'
        '{"type":"permission-mode","permissionMode":"acceptEdits"}\n'
        '{"message":{"model":"claude-sonnet-5"}}\n'
        '{"type":"permission-mode","permissionMode":"auto"}\n'
    )

    assert resolve_last_session_state(str(session_log)) == (
        "claude-sonnet-5",
        "auto",
    )


def test_resolve_last_session_state_uses_the_last_matching_entry_not_the_last_valid_one(
    tmp_path: Path,
) -> None:
    """The last-*seen* raw `permissionMode` value is validated once, not
    the last one that happened to be valid -- `auto` then `default`
    degrades to flag omission even though `auto` appeared earlier."""
    auto_then_default = tmp_path / "auto-then-default.jsonl"
    auto_then_default.write_text(
        '{"type":"permission-mode","permissionMode":"auto"}\n'
        '{"type":"permission-mode","permissionMode":"default"}\n'
    )
    default_then_auto = tmp_path / "default-then-auto.jsonl"
    default_then_auto.write_text(
        '{"type":"permission-mode","permissionMode":"default"}\n'
        '{"type":"permission-mode","permissionMode":"auto"}\n'
    )

    assert resolve_last_session_state(str(auto_then_default)) == (None, None)
    assert resolve_last_session_state(str(default_then_auto)) == (None, "auto")


def test_resolve_last_session_state_skips_a_malformed_json_line_but_keeps_scanning(
    tmp_path: Path,
) -> None:
    session_log = tmp_path / "session.jsonl"
    session_log.write_text(
        '{"message":{"model":"claude-opus-5"}}\n'
        "not valid json\n"
        '{"type":"permission-mode","permissionMode":"auto"}\n'
    )

    assert resolve_last_session_state(str(session_log)) == ("claude-opus-5", "auto")


def test_resolve_last_session_state_returns_model_only_when_no_permission_mode_entries_exist(
    tmp_path: Path,
) -> None:
    session_log = tmp_path / "session.jsonl"
    session_log.write_text('{"message":{"model":"claude-opus-5"}}\n')

    assert resolve_last_session_state(str(session_log)) == ("claude-opus-5", None)


def test_resolve_last_session_state_returns_permission_mode_only_when_no_model_entries_exist(
    tmp_path: Path,
) -> None:
    session_log = tmp_path / "session.jsonl"
    session_log.write_text('{"type":"permission-mode","permissionMode":"auto"}\n')

    assert resolve_last_session_state(str(session_log)) == (None, "auto")


def test_resolve_last_session_state_degrades_atomically_to_none_none_on_a_mid_scan_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read failure partway through a real scan (line 1 sets `model` for
    real; line 2's `extract_permission_mode()` call raises `OSError` from
    inside the live `with`/`for` loop) must discard whatever partial
    `model`/`permission_mode` had already been found -- never a
    half-valid result."""
    session_log = tmp_path / "session.jsonl"
    session_log.write_text(
        '{"message":{"model":"claude-opus-5"}}\n{"type":"agent-name"}\n'
    )
    calls = 0

    def flaky_extract_permission_mode(entry: object) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        raise OSError("disk read error mid-scan")

    monkeypatch.setattr(
        claude_projects_module,
        "extract_permission_mode",
        flaky_extract_permission_mode,
    )

    assert resolve_last_session_state(str(session_log)) == (None, None)


def test_resolve_last_session_state_returns_none_none_for_a_non_utf8_file(
    tmp_path: Path,
) -> None:
    session_log = tmp_path / "session.jsonl"
    session_log.write_bytes(b"\xff\xfe\x00\x01invalid utf-8")

    assert resolve_last_session_state(str(session_log)) == (None, None)


def test_resolve_last_session_state_returns_none_model_when_every_model_entry_is_synthetic(
    tmp_path: Path,
) -> None:
    session_log = tmp_path / "session.jsonl"
    session_log.write_text(
        '{"message":{"model":"<synthetic>"}}\n{"message":{"model":"<synthetic>"}}\n'
    )

    assert resolve_last_session_state(str(session_log)) == (None, None)


def test_resolve_last_session_state_skips_a_trailing_synthetic_entry_and_recovers_the_earlier_real_model(
    tmp_path: Path,
) -> None:
    session_log = tmp_path / "session.jsonl"
    session_log.write_text(
        '{"message":{"model":"claude-opus-5"}}\n{"message":{"model":"<synthetic>"}}\n'
    )

    assert resolve_last_session_state(str(session_log)) == ("claude-opus-5", None)

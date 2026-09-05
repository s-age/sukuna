from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState
from sukuna.domain.service.worker_record_session_log import (
    attach_session_log_path_if_unresolved,
)


def worker(**overrides) -> WorkerRecord:
    record = WorkerRecord.create(
        name="ccw-x-review-1", repo_root="/repo", worktree="/repo"
    )
    record.state = overrides.get("state", WorkerState.READY)
    record.session_log_path = overrides.get("session_log_path")
    record.session_log_reset_at = overrides.get("session_log_reset_at")
    return record


def test_leaves_an_already_resolved_record_untouched_and_never_calls_resolver() -> None:
    record = worker(session_log_path="/logs/existing.jsonl")
    calls: list[tuple[str, str, str | None]] = []

    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        calls.append((worktree, name, not_before))
        return "/logs/should-not-be-used.jsonl"

    attach_session_log_path_if_unresolved(record, resolver)

    assert record.session_log_path == "/logs/existing.jsonl"
    assert calls == []


def test_sets_session_log_path_when_resolver_finds_a_match() -> None:
    record = worker(session_log_path=None)

    attach_session_log_path_if_unresolved(
        record, lambda worktree, name, not_before: "/logs/found.jsonl"
    )

    assert record.session_log_path == "/logs/found.jsonl"


def test_leaves_session_log_path_none_when_resolver_finds_nothing() -> None:
    record = worker(session_log_path=None)

    attach_session_log_path_if_unresolved(
        record, lambda worktree, name, not_before: None
    )

    assert record.session_log_path is None


def test_starting_state_never_calls_the_resolver() -> None:
    """Respawn's STARTING transition just cleared session_log_path; the
    hook must not immediately try to re-resolve it before a fresh session
    file exists."""
    record = worker(state=WorkerState.STARTING, session_log_path=None)
    calls: list[tuple[str, str, str | None]] = []

    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        calls.append((worktree, name, not_before))
        return "/logs/should-not-be-used.jsonl"

    attach_session_log_path_if_unresolved(record, resolver)

    assert record.session_log_path is None
    assert calls == []


def test_passes_session_log_reset_at_through_to_the_resolver_as_third_argument() -> (
    None
):
    record = worker(
        session_log_path=None, session_log_reset_at="2026-08-25T00:00:00+00:00"
    )
    captured: dict[str, object] = {}

    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        captured["not_before"] = not_before
        return None

    attach_session_log_path_if_unresolved(record, resolver)

    assert captured["not_before"] == "2026-08-25T00:00:00+00:00"


def test_clears_session_log_reset_at_once_the_resolver_finds_a_match() -> None:
    record = worker(
        session_log_path=None, session_log_reset_at="2026-08-25T00:00:00+00:00"
    )

    attach_session_log_path_if_unresolved(
        record, lambda worktree, name, not_before: "/logs/found.jsonl"
    )

    assert record.session_log_path == "/logs/found.jsonl"
    assert record.session_log_reset_at is None


def test_keeps_session_log_reset_at_when_the_resolver_still_finds_nothing() -> None:
    record = worker(
        session_log_path=None, session_log_reset_at="2026-08-25T00:00:00+00:00"
    )

    attach_session_log_path_if_unresolved(
        record, lambda worktree, name, not_before: None
    )

    assert record.session_log_path is None
    assert record.session_log_reset_at == "2026-08-25T00:00:00+00:00"

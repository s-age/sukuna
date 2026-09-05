import dataclasses
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sukuna.cli import main
from sukuna.domain.entity.worker_record import WorkerRecord, WorkerState, utc_now
from sukuna.domain.mapper.registry_mapper import (
    CURRENT_SCHEMA_VERSION,
    WorkerRecordDocument,
    parse_registry_document,
    record_from_dict,
    record_to_dict,
)
from sukuna.errors import (
    ConflictError,
    NotFoundError,
    RegistryDocumentError,
    SchemaVersionError,
    StateError,
)
from sukuna.infrastructure.registry import Registry, sukuna_state_dir


def worker() -> WorkerRecord:
    return WorkerRecord.create(
        name="ccw-project-00000000-review-1",
        repo_root="/repo",
        worktree="/repo",
    )


def test_sukuna_state_dir_defaults_under_application_support_on_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")

    assert (
        sukuna_state_dir() == Path.home() / "Library" / "Application Support" / "sukuna"
    )


@pytest.mark.parametrize("platform", ["linux", "linux2", "freebsd13"])
def test_sukuna_state_dir_defaults_under_local_state_off_darwin(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", platform)

    assert sukuna_state_dir() == Path.home() / ".local" / "state" / "sukuna"


def test_sukuna_state_dir_honors_xdg_state_home_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert sukuna_state_dir() == tmp_path / "sukuna"


def test_registry_default_path_is_derived_from_sukuna_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert Registry().path == sukuna_state_dir() / "registry.json"


def test_registry_persists_and_enforces_transitions(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state" / "registry.json")
    registry.add(worker())
    name = "ccw-project-00000000-review-1"

    assert registry.get(name).state is WorkerState.STARTING
    assert registry.transition(name, WorkerState.READY).state is WorkerState.READY
    assert registry.transition(name, WorkerState.BUSY).state is WorkerState.BUSY
    assert registry.transition(name, WorkerState.REPORTED).state is WorkerState.REPORTED
    accepted = registry.transition(name, WorkerState.ACCEPTED)

    assert accepted.may_close is False
    assert (tmp_path / "state" / "registry.json").exists()


def test_reported_worker_can_return_to_busy_and_back(tmp_path: Path) -> None:
    """Sending a REPORTED worker additional work (review-finding fixes,
    revisions) must be representable as REPORTED -> BUSY -> REPORTED, not
    just a one-way trip to ACCEPTED."""
    registry = Registry(tmp_path / "registry.json")
    registry.add(worker())
    name = "ccw-project-00000000-review-1"
    for state in (WorkerState.READY, WorkerState.BUSY, WorkerState.REPORTED):
        registry.transition(name, state)

    assert registry.transition(name, WorkerState.BUSY).state is WorkerState.BUSY
    assert registry.transition(name, WorkerState.REPORTED).state is WorkerState.REPORTED


def test_invalid_transition_is_rejected(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    registry.add(worker())

    with pytest.raises(StateError):
        registry.transition("ccw-project-00000000-review-1", WorkerState.CLOSED)


def test_transition_bumps_updated_at() -> None:
    record = worker()
    created = record.updated_at

    record.transition_to(WorkerState.READY)

    assert record.updated_at != created


def test_managed_accepted_worker_with_pane_may_close() -> None:
    record = worker()
    record.pane_ref = "w0t0p0"
    record.state = WorkerState.ACCEPTED

    assert record.may_close is True


def test_accept_command_transitions_a_reported_worker(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    record = worker()
    registry.add(record)
    for state in (WorkerState.READY, WorkerState.BUSY, WorkerState.REPORTED):
        registry.transition(record.name, state)

    assert (
        main(["--registry", str(registry_path), "accept", "--worker", record.name]) == 0
    )
    assert registry.get(record.name).state is WorkerState.ACCEPTED


def test_from_dict_migrates_the_original_shape_before_ca61472() -> None:
    """M-1: the very first on-disk shape (worker_id/run_id/backend/role,
    created_at, peer_id; no goal/parent_session_id) still loads, and
    peer_id's value is carried over into parent_session_id."""
    legacy = {
        "worker_id": "wid-1",
        "run_id": "rid-1",
        "name": "ccw-project-00000000-review-1",
        "role": "review",
        "repo_root": "/repo",
        "worktree": "/repo",
        "created_at": "2026-08-01T00:00:00+00:00",
        "state": "starting",
        "pane_ref": None,
        "window_ref": None,
        "managed": True,
        "peer_id": "peer-session-xyz",
        "backend": "iterm2",
    }

    record = record_from_dict(legacy)

    assert record.updated_at == "2026-08-01T00:00:00+00:00"
    assert record.name == "ccw-project-00000000-review-1"
    assert record.goal is None
    assert record.parent_session_id == "peer-session-xyz"
    assert not hasattr(record, "role")
    assert not hasattr(record, "worker_id")
    assert not hasattr(record, "run_id")
    assert not hasattr(record, "backend")
    assert not hasattr(record, "peer_id")


def test_from_dict_migrates_iterm_session_id_and_window_id() -> None:
    """An accepted worker restored from the pre-rename
    iterm_session_id/iterm_window_id shape must regain a truthy pane_ref,
    so may_close becomes True again instead of staying stuck False
    forever."""
    legacy = {
        "name": "ccw-project-00000000-review-1",
        "repo_root": "/repo",
        "worktree": "/repo",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "state": "accepted",
        "iterm_session_id": "ABCDEF12-3456-7890-ABCD-EF1234567890",
        "iterm_window_id": "w0",
        "managed": True,
    }

    record = record_from_dict(legacy)

    assert record.pane_ref == "ABCDEF12-3456-7890-ABCD-EF1234567890"
    assert record.window_ref == "w0"
    assert not hasattr(record, "iterm_session_id")
    assert not hasattr(record, "iterm_window_id")
    assert record.may_close is True


def test_from_dict_migrates_the_shape_before_403d166() -> None:
    """M-2: the shape right before worker_id/run_id/backend were dropped
    (parent_session_id already present, role/created_at still present)."""
    legacy = {
        "worker_id": "wid-2",
        "run_id": "rid-2",
        "name": "ccw-project-00000000-review-2",
        "role": "review",
        "repo_root": "/repo",
        "worktree": "/repo",
        "created_at": "2026-08-02T00:00:00+00:00",
        "state": "ready",
        "pane_ref": "w0t0p0",
        "window_ref": None,
        "managed": True,
        "parent_session_id": "session-abc",
        "backend": "tmux",
    }

    record = record_from_dict(legacy)

    assert record.updated_at == "2026-08-02T00:00:00+00:00"
    assert record.parent_session_id == "session-abc"
    assert record.state is WorkerState.READY
    assert not hasattr(record, "worker_id")
    assert not hasattr(record, "run_id")
    assert not hasattr(record, "backend")


def test_read_unlocked_rejects_a_future_schema_version(tmp_path: Path) -> None:
    """M-3."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps({"schema_version": CURRENT_SCHEMA_VERSION + 999, "workers": []}),
        encoding="utf-8",
    )
    registry = Registry(registry_path)

    with pytest.raises(SchemaVersionError):
        registry.list()


def test_parse_registry_document_rejects_a_future_schema_version_directly() -> None:
    """F14: this check now lives in `parse_registry_document()` itself, not
    behind `Registry._read_unlocked()` -- callable and testable on its own."""
    with pytest.raises(SchemaVersionError):
        parse_registry_document(
            {"schema_version": CURRENT_SCHEMA_VERSION + 999, "workers": []}
        )


def test_parse_registry_document_rejects_a_non_dict_root_directly() -> None:
    with pytest.raises(RegistryDocumentError):
        parse_registry_document([1, 2, 3])  # type: ignore[arg-type]


def test_parse_registry_document_rejects_a_non_int_schema_version_directly() -> None:
    with pytest.raises(RegistryDocumentError):
        parse_registry_document({"schema_version": "3", "workers": []})


def test_parse_registry_document_rejects_a_bool_schema_version() -> None:
    """`strict=True` on `schema_version` rejects bool (bool is an int
    subclass in Python)."""
    with pytest.raises(RegistryDocumentError):
        parse_registry_document({"schema_version": True, "workers": []})


def test_parse_registry_document_prefers_schema_version_ceiling_over_workers_shape() -> (
    None
):
    """A newer schema version may have legitimately restructured `workers`;
    this sukuna can't judge that shape, so the ceiling check must win over
    any complaint about `workers` not being a list."""
    with pytest.raises(SchemaVersionError):
        parse_registry_document(
            {"schema_version": CURRENT_SCHEMA_VERSION + 999, "workers": None}
        )


def test_read_unlocked_treats_a_missing_schema_version_as_version_one(
    tmp_path: Path,
) -> None:
    """M-4: registry.json written before this migration had no
    schema_version key at all and must still load."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "workers": [
                    {
                        "name": "ccw-project-00000000-review-1",
                        "repo_root": "/repo",
                        "worktree": "/repo",
                        "updated_at": "2026-08-01T00:00:00+00:00",
                        "state": "starting",
                        "pane_ref": None,
                        "window_ref": None,
                        "managed": True,
                        "parent_session_id": None,
                        "goal": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    registry = Registry(registry_path)

    workers = registry.list()

    assert len(workers) == 1
    assert workers[0].name == "ccw-project-00000000-review-1"


def test_mutate_applies_the_mutation_and_persists(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    registry.add(worker())
    name = "ccw-project-00000000-review-1"

    result = registry.mutate(name, lambda record: setattr(record, "goal", "new goal"))

    assert result.goal == "new goal"
    assert registry.get(name).goal == "new goal"


def test_mutate_raises_not_found_for_an_unregistered_name(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")

    with pytest.raises(NotFoundError):
        registry.mutate("no-such-worker", lambda record: None)


def test_mutate_does_not_write_when_the_mutation_raises(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    registry.add(worker())
    name = "ccw-project-00000000-review-1"

    def _boom(record: WorkerRecord) -> None:
        record.goal = "should not persist"
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        registry.mutate(name, _boom)

    assert registry.get(name).goal is None


def test_mutate_succeeds_when_expected_updated_at_matches(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    record = worker()
    registry.add(record)

    result = registry.mutate(
        record.name,
        lambda w: setattr(w, "goal", "updated"),
        expected_updated_at=record.updated_at,
    )

    assert result.goal == "updated"
    assert registry.get(record.name).goal == "updated"


def test_mutate_rejects_a_stale_expected_updated_at(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    record = worker()
    registry.add(record)
    stale_updated_at = record.updated_at
    registry.transition(record.name, WorkerState.READY)  # bumps updated_at concurrently

    with pytest.raises(ConflictError):
        registry.mutate(
            record.name,
            lambda w: setattr(w, "goal", "should not persist"),
            expected_updated_at=stale_updated_at,
        )

    assert registry.get(record.name).goal is None
    assert registry.get(record.name).state is WorkerState.READY


def test_transition_succeeds_when_expected_updated_at_matches(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    record = worker()
    registry.add(record)

    result = registry.transition(
        record.name, WorkerState.READY, expected_updated_at=record.updated_at
    )

    assert result.state is WorkerState.READY


def test_transition_rejects_a_stale_expected_updated_at(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    record = worker()
    registry.add(record)
    stale_updated_at = record.updated_at
    registry.transition(record.name, WorkerState.READY)  # bumps updated_at concurrently

    with pytest.raises(ConflictError):
        registry.transition(
            record.name, WorkerState.BUSY, expected_updated_at=stale_updated_at
        )

    assert registry.get(record.name).state is WorkerState.READY


def test_replace_succeeds_when_expected_updated_at_matches(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    record = worker()
    registry.add(record)

    record.goal = "updated"
    registry.replace(record, expected_updated_at=record.updated_at)

    assert registry.get(record.name).goal == "updated"


def test_replace_rejects_a_stale_expected_updated_at(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    record = worker()
    registry.add(record)
    stale_updated_at = record.updated_at
    registry.transition(record.name, WorkerState.READY)  # bumps updated_at concurrently

    record.goal = "should not persist"
    with pytest.raises(ConflictError):
        registry.replace(record, expected_updated_at=stale_updated_at)

    assert registry.get(record.name).goal is None
    assert registry.get(record.name).state is WorkerState.READY


def test_replace_without_expected_updated_at_overwrites_unconditionally(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    record = worker()
    registry.add(record)
    registry.transition(record.name, WorkerState.READY)  # bumps updated_at concurrently

    record.goal = "clobbered anyway"
    registry.replace(record)

    assert registry.get(record.name).goal == "clobbered anyway"


def test_write_unlocked_stamps_the_current_schema_version(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    registry.add(worker())

    payload = json.loads(registry_path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == CURRENT_SCHEMA_VERSION == 3


def test_list_raises_registry_document_error_for_invalid_json(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{not valid json", encoding="utf-8")
    registry = Registry(registry_path)

    with pytest.raises(RegistryDocumentError) as excinfo:
        registry.list()

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_list_raises_registry_document_error_for_a_missing_required_key(
    tmp_path: Path,
) -> None:
    """`state` has no default; a record without it must fail validation
    rather than raise a raw KeyError."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "workers": [
                    {
                        "name": "ccw-project-00000000-review-1",
                        "repo_root": "/repo",
                        "worktree": "/repo",
                        "updated_at": "2026-08-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = Registry(registry_path)

    with pytest.raises(RegistryDocumentError) as excinfo:
        registry.list()

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_list_raises_registry_document_error_for_a_malformed_field_type(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "workers": [
                    {
                        "name": "ccw-project-00000000-review-1",
                        "repo_root": "/repo",
                        "worktree": "/repo",
                        "updated_at": "2026-08-01T00:00:00+00:00",
                        "state": "not-a-real-state",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = Registry(registry_path)

    with pytest.raises(RegistryDocumentError) as excinfo:
        registry.list()

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_parse_registry_document_rejects_a_name_that_violates_the_naming_convention() -> (
    None
):
    """Same `_NAME_RE` boundary as `record_from_dict()`, exercised through
    the whole-registry entry point rather than the single-record one."""
    with pytest.raises(RegistryDocumentError) as excinfo:
        parse_registry_document(
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "workers": [
                    {
                        "name": "Not A Valid Name!",
                        "repo_root": "/repo",
                        "worktree": "/repo",
                        "updated_at": "2026-08-01T00:00:00+00:00",
                        "state": "starting",
                    }
                ],
            }
        )

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_list_raises_registry_document_error_for_duplicate_worker_names(
    tmp_path: Path,
) -> None:
    """Two records sharing the same `name` (registry's sole primary key)
    must fail validation rather than silently collapse to one record
    (later-wins) via `records_from_document()`'s dict comprehension."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "workers": [
                    {
                        "name": "ccw-project-00000000-review-1",
                        "repo_root": "/repo",
                        "worktree": "/repo",
                        "updated_at": "2026-08-01T00:00:00+00:00",
                        "state": "ready",
                        "goal": "first",
                    },
                    {
                        "name": "ccw-project-00000000-review-1",
                        "repo_root": "/repo",
                        "worktree": "/repo",
                        "updated_at": "2026-08-01T00:00:00+00:00",
                        "state": "ready",
                        "goal": "second",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = Registry(registry_path)

    with pytest.raises(RegistryDocumentError) as excinfo:
        registry.list()

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"
    assert "ccw-project-00000000-review-1" in str(excinfo.value)


def test_list_raises_registry_document_error_when_workers_is_not_a_list(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps({"schema_version": CURRENT_SCHEMA_VERSION, "workers": None}),
        encoding="utf-8",
    )
    registry = Registry(registry_path)

    with pytest.raises(RegistryDocumentError) as excinfo:
        registry.list()

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_list_raises_registry_document_error_when_json_root_is_not_an_object(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    registry = Registry(registry_path)

    with pytest.raises(RegistryDocumentError) as excinfo:
        registry.list()

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_list_raises_registry_document_error_for_an_unknown_top_level_key(
    tmp_path: Path,
) -> None:
    """The top-level `extra="forbid"` must actually see unknown keys."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {"schema_version": CURRENT_SCHEMA_VERSION, "workers": [], "typo": True}
        ),
        encoding="utf-8",
    )
    registry = Registry(registry_path)

    with pytest.raises(RegistryDocumentError) as excinfo:
        registry.list()

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_list_raises_registry_document_error_for_a_non_dict_worker_element(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps({"schema_version": CURRENT_SCHEMA_VERSION, "workers": [None]}),
        encoding="utf-8",
    )
    registry = Registry(registry_path)

    with pytest.raises(RegistryDocumentError) as excinfo:
        registry.list()

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_list_raises_registry_document_error_for_a_non_int_schema_version(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps({"schema_version": "3", "workers": []}),
        encoding="utf-8",
    )
    registry = Registry(registry_path)

    with pytest.raises(RegistryDocumentError) as excinfo:
        registry.list()

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_write_unlocked_fsyncs_the_temp_file_before_the_atomic_rename(
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
    registry = Registry(tmp_path / "registry.json")

    registry.add(worker())

    assert len(calls) == 1


def test_list_raises_registry_document_error_for_a_non_iso_updated_at(
    tmp_path: Path,
) -> None:
    """A hand-edited registry with a non-ISO `updated_at` must be rejected
    at the boundary rather than reaching `sukuna-cli tree`'s
    `datetime.fromisoformat()` as a raw `ValueError`."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "workers": [
                    {
                        "name": "ccw-project-00000000-review-1",
                        "repo_root": "/repo",
                        "worktree": "/repo",
                        "updated_at": "yesterday",
                        "state": "starting",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = Registry(registry_path)

    with pytest.raises(RegistryDocumentError) as excinfo:
        registry.list()

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_record_from_dict_rejects_a_naive_updated_at(tmp_path: Path) -> None:
    """A naive ISO string (no tzinfo) parses fine but would compare unsafely
    against the aware `--since`/`--to` datetimes in `worker_tree.py` — must
    be rejected here rather than surfacing as a `TypeError` later."""
    value = {
        "name": "ccw-project-00000000-review-1",
        "repo_root": "/repo",
        "worktree": "/repo",
        "updated_at": "2026-08-20T00:00:00",
        "state": "starting",
    }

    with pytest.raises(RegistryDocumentError) as excinfo:
        record_from_dict(value)

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_record_from_dict_rejects_an_empty_name(tmp_path: Path) -> None:
    value = {
        "name": "",
        "repo_root": "/repo",
        "worktree": "/repo",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "state": "starting",
    }

    with pytest.raises(RegistryDocumentError) as excinfo:
        record_from_dict(value)

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_record_from_dict_rejects_an_empty_parent_worker_name(tmp_path: Path) -> None:
    """ "no parent" has exactly one spelling (`None`, the default) — an
    empty string can only arrive via hand-edit (`spawn`'s `validate_name()`
    blocks it) and would otherwise make `root_children()`/`build_tree()`
    treat the record as a root while `children_of()`'s exact match can
    never find it as a child."""
    value = {
        "name": "ccw-project-00000000-review-1",
        "repo_root": "/repo",
        "worktree": "/repo",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "state": "starting",
        "parent_worker_name": "",
    }

    with pytest.raises(RegistryDocumentError) as excinfo:
        record_from_dict(value)

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


@pytest.mark.parametrize(
    "empty_field",
    ["pane_ref", "window_ref", "repo_root", "worktree", "session_log_path"],
)
def test_record_from_dict_rejects_an_empty_string_in_a_pane_or_path_field(
    empty_field: str,
) -> None:
    """An empty `pane_ref` can only arrive via hand-edit (`SpawnResult`
    already enforces `min_length=1` on the write path) and would otherwise
    split the liveness predicates — counted alive by `alive_pane_holders()`
    (`is not None`) yet neither closable (`may_close`'s truthiness) nor
    respawnable (`may_respawn`'s `is None`) — permanently blocking the
    parent's close, with `infer_backend("")` mis-routing verification to
    iterm2 so tmux-only environments cannot reconcile it away. Same
    boundary treatment for the other four fields -- `session_log_path`'s
    own failure mode is `attach_session_log_path_if_unresolved()`'s
    `is not None` guard latching onto the empty string forever."""
    value = {
        "name": "ccw-project-00000000-review-1",
        "repo_root": "/repo",
        "worktree": "/repo",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "state": "starting",
        empty_field: "",
    }

    with pytest.raises(RegistryDocumentError) as excinfo:
        record_from_dict(value)

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


@pytest.mark.parametrize(
    "invalid_name",
    [
        "Not A Valid Name!",
        "UPPERCASE",
        "has spaces",
        "has_underscore",
        "a" * 97,
    ],
)
def test_record_from_dict_rejects_a_name_that_violates_the_naming_convention(
    invalid_name: str,
) -> None:
    """`name` doubles as the `claude -n`/`SendMessage`/`--resume` session
    name (CLAUDE.md); a hand-edited name that violates
    `command_mapper.validate_name()`'s `_NAME_RE` must be caught here rather
    than surfacing later as a raw `ValidationError` out of
    `resume_command()`."""
    value = {
        "name": invalid_name,
        "repo_root": "/repo",
        "worktree": "/repo",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "state": "starting",
    }

    with pytest.raises(RegistryDocumentError) as excinfo:
        record_from_dict(value)

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


@pytest.mark.parametrize(
    "invalid_parent_worker_name",
    ["Not A Valid Name!", "UPPERCASE", "has spaces", "a" * 97],
)
def test_record_from_dict_rejects_a_parent_worker_name_that_violates_the_naming_convention(
    invalid_parent_worker_name: str,
) -> None:
    value = {
        "name": "ccw-project-00000000-review-1",
        "repo_root": "/repo",
        "worktree": "/repo",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "state": "starting",
        "parent_worker_name": invalid_parent_worker_name,
    }

    with pytest.raises(RegistryDocumentError) as excinfo:
        record_from_dict(value)

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_record_from_dict_rejects_an_unknown_key_that_survives_legacy_migration(
    tmp_path: Path,
) -> None:
    """A typo'd or hand-edited key that is not one of the known legacy
    renames/drops must be caught, not silently ignored (`extra="forbid"`)."""
    value = {
        "name": "ccw-project-00000000-review-1",
        "repo_root": "/repo",
        "worktree": "/repo",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "state": "starting",
        "totally_unknown_field": "surprise",
    }

    with pytest.raises(RegistryDocumentError) as excinfo:
        record_from_dict(value)

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_add_translates_a_pydantic_validation_error_for_an_empty_name(
    tmp_path: Path,
) -> None:
    """`Registry.add()` funnels through `_write_unlocked()` ->
    `document_from_records()`; a `WorkerRecord` built directly (bypassing
    `validate_name()`, the only guard on the normal `spawn` path) with an
    empty `name` must surface as `RegistryDocumentError`, not a raw pydantic
    `ValidationError` -- the write-side counterpart of `record_from_dict`'s
    read-side translation."""
    registry = Registry(tmp_path / "state" / "registry.json")
    invalid = WorkerRecord(
        name="",
        repo_root="/repo",
        worktree="/repo",
        updated_at=utc_now(),
        state=WorkerState.STARTING,
    )

    with pytest.raises(RegistryDocumentError) as excinfo:
        registry.add(invalid)

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_record_to_dict_translates_a_pydantic_validation_error_for_an_empty_name() -> (
    None
):
    """`record_to_dict()`'s callers (`close.py`, `accept.py`, `inspect.py`,
    `spawn.py`, `state.py`, `reconcile.py`) all pass already-validated
    records, so this is currently unreachable in production — but a
    dataclass-direct `WorkerRecord` (bypassing `validate_name()`) must still
    surface as `RegistryDocumentError`, not a raw pydantic `ValidationError`
    -- the single-record counterpart of
    `test_add_translates_a_pydantic_validation_error_for_an_empty_name`."""
    invalid = WorkerRecord(
        name="",
        repo_root="/repo",
        worktree="/repo",
        updated_at=utc_now(),
        state=WorkerState.STARTING,
    )

    with pytest.raises(RegistryDocumentError) as excinfo:
        record_to_dict(invalid)

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_worker_record_and_worker_record_document_have_the_same_field_set() -> None:
    """`WorkerRecord` (dataclass), `WorkerRecordDocument` (pydantic), and the
    hand-written kwargs listings in `_document_from_record()`/
    `_record_from_document()` are four independent, hand-maintained copies
    of the same field set. This pins the two field-set declarations to
    each other so a field added to one and forgotten in the other fails
    loudly here instead of silently dropping data at the
    `_document_from_record()`/`_record_from_document()` kwargs boundary."""
    record_fields = {field.name for field in dataclasses.fields(WorkerRecord)}
    document_fields = set(WorkerRecordDocument.model_fields)

    assert record_fields == document_fields


def test_record_round_trips_through_record_to_dict_and_record_from_dict() -> None:
    """Every field set to a non-default value, so a kwarg dropped from
    `_document_from_record()` or `_record_from_document()` surfaces as a
    round-trip mismatch rather than accidentally matching a default."""
    original = WorkerRecord(
        name="ccw-project-00000000-review-1",
        repo_root="/repo",
        worktree="/repo/worktree",
        updated_at="2026-08-01T00:00:00+00:00",
        state=WorkerState.BUSY,
        pane_ref="%5",
        window_ref="@2",
        managed=False,
        parent_session_id="parent-session-uuid",
        goal="test goal",
        parent_worker_name="ccw-project-00000000-review-0",
        model="claude-sonnet-5",
        session_log_path="/logs/session.jsonl",
        session_log_reset_at="2026-08-25T00:00:00+00:00",
    )

    for field in dataclasses.fields(WorkerRecord):
        if field.default is not dataclasses.MISSING:
            assert getattr(original, field.name) != field.default, (
                f"{field.name} must differ from its default for this test to "
                "catch a dropped kwarg in _document_from_record()/"
                "_record_from_document()"
            )

    round_tripped = record_from_dict(record_to_dict(original))

    assert round_tripped == original


def test_add_translates_a_pydantic_validation_error_for_an_empty_parent_worker_name(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "state" / "registry.json")
    invalid = WorkerRecord(
        name="ccw-project-00000000-review-1",
        repo_root="/repo",
        worktree="/repo",
        updated_at=utc_now(),
        state=WorkerState.STARTING,
        parent_worker_name="",
    )

    with pytest.raises(RegistryDocumentError) as excinfo:
        registry.add(invalid)

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


# -- session_log_path / session_log_reset_at --


def test_record_from_dict_migrates_a_shape_missing_session_log_fields() -> None:
    """Every on-disk record written before this card lacks both new keys
    entirely -- pydantic's `Field(default=None, ...)` must fill them in,
    the same backward-compat pattern the `model` field addition used."""
    legacy = {
        "name": "ccw-project-00000000-review-1",
        "repo_root": "/repo",
        "worktree": "/repo",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "state": "starting",
    }

    record = record_from_dict(legacy)

    assert record.session_log_path is None
    assert record.session_log_reset_at is None


@pytest.mark.parametrize(
    "invalid_value",
    ["not-a-real-timestamp", "2026-08-20T00:00:00"],
    ids=["non-iso-string", "naive-datetime-string"],
)
def test_record_from_dict_rejects_an_invalid_session_log_reset_at(
    invalid_value: str,
) -> None:
    value = {
        "name": "ccw-project-00000000-review-1",
        "repo_root": "/repo",
        "worktree": "/repo",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "state": "starting",
        "session_log_reset_at": invalid_value,
    }

    with pytest.raises(RegistryDocumentError) as excinfo:
        record_from_dict(value)

    assert excinfo.value.code == "REGISTRY_DOCUMENT_ERROR"


def test_record_from_dict_accepts_a_none_session_log_reset_at() -> None:
    value = {
        "name": "ccw-project-00000000-review-1",
        "repo_root": "/repo",
        "worktree": "/repo",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "state": "starting",
        "session_log_reset_at": None,
    }

    record = record_from_dict(value)

    assert record.session_log_reset_at is None


def test_mutate_resolves_an_unresolved_workers_session_log_path(
    tmp_path: Path,
) -> None:
    """The general `mutate()` hook: an unresolved worker going through any
    transition gets `session_log_path` filled in by the injected
    resolver."""
    registry = Registry(
        tmp_path / "registry.json",
        session_log_resolver=lambda worktree, name, not_before: "/logs/found.jsonl",
    )
    registry.add(worker())
    name = "ccw-project-00000000-review-1"

    result = registry.transition(name, WorkerState.READY)

    assert result.session_log_path == "/logs/found.jsonl"
    assert registry.get(name).session_log_path == "/logs/found.jsonl"


def test_mutate_does_not_call_the_resolver_for_an_already_resolved_worker(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, str | None]] = []

    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        calls.append((worktree, name, not_before))
        return "/logs/should-not-be-set.jsonl"

    registry = Registry(tmp_path / "registry.json", session_log_resolver=resolver)
    record = worker()
    record.session_log_path = "/logs/already-resolved.jsonl"
    registry.add(record)
    name = "ccw-project-00000000-review-1"

    registry.transition(name, WorkerState.READY)

    assert calls == []
    assert registry.get(name).session_log_path == "/logs/already-resolved.jsonl"


def test_mutate_does_not_call_the_resolver_for_a_transition_into_starting(
    tmp_path: Path,
) -> None:
    """Respawn's FAILED -> STARTING transition must not immediately
    re-resolve a session_log_path the same closure just cleared (card
    §4.2/§4.7's STARTING guard)."""
    calls: list[tuple[str, str, str | None]] = []

    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        calls.append((worktree, name, not_before))
        return "/logs/should-not-be-set.jsonl"

    registry = Registry(tmp_path / "registry.json", session_log_resolver=resolver)
    record = worker()
    record.state = WorkerState.FAILED
    registry.add(record)
    name = "ccw-project-00000000-review-1"

    result = registry.transition(name, WorkerState.STARTING)

    assert calls == []
    assert result.session_log_path is None


def test_mutate_survives_a_resolver_oserror_and_leaves_the_transition_intact(
    tmp_path: Path,
) -> None:
    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        raise OSError("permission denied")

    registry = Registry(tmp_path / "registry.json", session_log_resolver=resolver)
    registry.add(worker())
    name = "ccw-project-00000000-review-1"

    result = registry.transition(name, WorkerState.READY)

    assert result.state is WorkerState.READY
    assert result.session_log_path is None


def test_mutate_survives_a_resolver_valueerror_and_leaves_the_transition_intact(
    tmp_path: Path,
) -> None:
    """Multiple-defense counterpart of the OSError case above: a malformed
    `session_log_reset_at` reaching `datetime.fromisoformat()` inside a
    resolver must not fail the transition either."""

    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        raise ValueError("invalid isoformat string")

    registry = Registry(tmp_path / "registry.json", session_log_resolver=resolver)
    registry.add(worker())
    name = "ccw-project-00000000-review-1"

    result = registry.transition(name, WorkerState.READY)

    assert result.state is WorkerState.READY
    assert result.session_log_path is None


def test_mutate_passes_session_log_reset_at_to_the_resolver(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        captured["not_before"] = not_before
        return None

    registry = Registry(tmp_path / "registry.json", session_log_resolver=resolver)
    record = worker()
    record.session_log_reset_at = "2026-08-25T00:00:00+00:00"
    registry.add(record)
    name = "ccw-project-00000000-review-1"

    registry.transition(name, WorkerState.READY)

    assert captured["not_before"] == "2026-08-25T00:00:00+00:00"


def test_replace_resolves_an_unresolved_workers_session_log_path(
    tmp_path: Path,
) -> None:
    """`replace()` is a separate persistence primitive from `mutate()` --
    the hook must be applied there too, not just in `mutate()`."""
    registry_path = tmp_path / "registry.json"
    unresolved_registry = Registry(
        registry_path, session_log_resolver=lambda worktree, name, not_before: None
    )
    record = worker()
    unresolved_registry.add(record)
    unresolved_registry.transition(record.name, WorkerState.READY)  # STARTING guard
    record = unresolved_registry.get(record.name)
    assert record.session_log_path is None

    resolving_registry = Registry(
        registry_path,
        session_log_resolver=lambda worktree, name, not_before: "/logs/found.jsonl",
    )
    record.goal = "updated via replace"
    resolving_registry.replace(record, expected_updated_at=record.updated_at)

    assert resolving_registry.get(record.name).session_log_path == "/logs/found.jsonl"


def test_replace_does_not_call_the_resolver_for_an_already_resolved_worker(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, str | None]] = []

    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        calls.append((worktree, name, not_before))
        return "/logs/should-not-be-set.jsonl"

    registry = Registry(tmp_path / "registry.json", session_log_resolver=resolver)
    record = worker()
    record.session_log_path = "/logs/already-resolved.jsonl"
    registry.add(record)

    record.goal = "updated via replace"
    registry.replace(record, expected_updated_at=record.updated_at)

    assert calls == []
    assert registry.get(record.name).session_log_path == "/logs/already-resolved.jsonl"


def test_replace_does_not_call_the_resolver_for_a_starting_worker(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, str | None]] = []

    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        calls.append((worktree, name, not_before))
        return "/logs/should-not-be-set.jsonl"

    registry = Registry(tmp_path / "registry.json", session_log_resolver=resolver)
    record = worker()  # WorkerRecord.create() always starts as STARTING
    registry.add(record)

    record.goal = "updated via replace"
    registry.replace(record, expected_updated_at=record.updated_at)

    assert calls == []


def test_replace_survives_a_resolver_oserror(tmp_path: Path) -> None:
    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        raise OSError("permission denied")

    registry = Registry(tmp_path / "registry.json", session_log_resolver=resolver)
    record = worker()
    registry.add(record)
    registry.transition(record.name, WorkerState.READY)
    record = registry.get(record.name)

    record.goal = "updated via replace"
    registry.replace(record, expected_updated_at=record.updated_at)

    assert registry.get(record.name).goal == "updated via replace"
    assert registry.get(record.name).session_log_path is None


def test_replace_survives_a_resolver_valueerror(tmp_path: Path) -> None:
    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        raise ValueError("invalid isoformat string")

    registry = Registry(tmp_path / "registry.json", session_log_resolver=resolver)
    record = worker()
    registry.add(record)
    registry.transition(record.name, WorkerState.READY)
    record = registry.get(record.name)

    record.goal = "updated via replace"
    registry.replace(record, expected_updated_at=record.updated_at)

    assert registry.get(record.name).goal == "updated via replace"
    assert registry.get(record.name).session_log_path is None


def test_replace_passes_session_log_reset_at_to_the_resolver(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def resolver(worktree: str, name: str, not_before: str | None) -> str | None:
        captured["not_before"] = not_before
        return None

    registry = Registry(tmp_path / "registry.json", session_log_resolver=resolver)
    record = worker()
    registry.add(record)
    registry.transition(record.name, WorkerState.READY)
    record = registry.get(record.name)
    record.session_log_reset_at = "2026-08-25T00:00:00+00:00"

    record.goal = "updated via replace"
    registry.replace(record, expected_updated_at=record.updated_at)

    assert captured["not_before"] == "2026-08-25T00:00:00+00:00"


# -- schema v3: ordinal_high_water_marks / last_swept_at / next_ordinal / purge --


def _closed_worker(
    name: str,
    *,
    parent_session_id: str | None,
    updated_at: str,
    state: WorkerState = WorkerState.CLOSED,
    pane_ref: str | None = None,
) -> WorkerRecord:
    record = WorkerRecord.create(
        name=name,
        repo_root="/repo",
        worktree="/repo",
        parent_session_id=parent_session_id,
    )
    record.state = state
    record.updated_at = updated_at
    record.pane_ref = pane_ref
    return record


def test_next_ordinal_returns_one_for_a_session_with_no_records(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")

    assert registry.next_ordinal("session-a") == 1


def test_next_ordinal_counts_existing_records_for_the_session(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.json")
    registry.add(
        _closed_worker("ccw-a-1", parent_session_id="session-a", updated_at=utc_now())
    )

    assert registry.next_ordinal("session-a") == 2


def test_add_with_claim_ordinal_advances_the_high_water_mark(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    record = worker()
    record.parent_session_id = "session-a"

    registry.add(record, claim_ordinal=1)

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["ordinal_high_water_marks"] == {"session-a": 2}
    assert registry.next_ordinal("session-a") == 2


def test_add_without_claim_ordinal_does_not_touch_the_high_water_mark(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    registry.add(worker())

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["ordinal_high_water_marks"] == {}


def test_add_claim_ordinal_never_lowers_an_existing_floor(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    first = worker()
    first.parent_session_id = "session-a"
    registry.add(first, claim_ordinal=5)

    second = _closed_worker(
        "ccw-project-00000000-review-2",
        parent_session_id="session-a",
        updated_at=utc_now(),
    )
    registry.add(second, claim_ordinal=1)

    assert registry.next_ordinal("session-a") == 6


def test_read_unlocked_treats_a_missing_ordinal_high_water_marks_as_a_v2_registry(
    tmp_path: Path,
) -> None:
    """A v2 registry.json (schema_version 2, no `ordinal_high_water_marks`/
    `last_swept_at` keys at all) must still load -- the two new fields
    default to `{}`/`None` via pydantic, no `_migrate_legacy_fields()`
    entry needed."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "workers": [
                    {
                        "name": "ccw-project-00000000-review-1",
                        "repo_root": "/repo",
                        "worktree": "/repo",
                        "updated_at": "2026-08-01T00:00:00+00:00",
                        "state": "closed",
                        "parent_session_id": "session-a",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = Registry(registry_path)

    assert registry.next_ordinal("session-a") == 2
    (record,) = registry.list()
    assert record.name == "ccw-project-00000000-review-1"


def test_write_unlocked_stamps_ordinal_high_water_marks_and_last_swept_at_round_trip(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    record = worker()
    record.parent_session_id = "session-a"
    registry.add(record, claim_ordinal=1)

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["ordinal_high_water_marks"] == {"session-a": 2}
    assert payload["last_swept_at"] is None


def test_purge_removes_records_older_than_retention_days(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    now = datetime(2026, 8, 30, tzinfo=UTC)
    old = _closed_worker(
        "ccw-project-00000000-review-1",
        parent_session_id="session-a",
        updated_at=(now - timedelta(days=31)).isoformat(),
    )
    recent = _closed_worker(
        "ccw-project-00000000-review-2",
        parent_session_id="session-a",
        updated_at=(now - timedelta(days=1)).isoformat(),
    )
    registry.add(old)
    registry.add(recent)

    result = registry.purge(retention_days=30, now=now)

    assert result.swept is True
    assert [record.name for record in result.purged] == [old.name]
    remaining = {record.name for record in registry.list()}
    assert remaining == {recent.name}


def test_purge_keeps_a_failed_worker_with_a_live_pane(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    now = datetime(2026, 8, 30, tzinfo=UTC)
    failed_with_pane = _closed_worker(
        "ccw-project-00000000-review-1",
        parent_session_id="session-a",
        updated_at=(now - timedelta(days=31)).isoformat(),
        state=WorkerState.FAILED,
        pane_ref="%5",
    )
    registry.add(failed_with_pane)

    result = registry.purge(retention_days=30, now=now)

    assert result.purged == []
    assert [record.name for record in registry.list()] == [failed_with_pane.name]


def test_purge_throttles_to_once_per_utc_day_unless_forced(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.json")
    now = datetime(2026, 8, 30, 1, tzinfo=UTC)
    old = _closed_worker(
        "ccw-project-00000000-review-1",
        parent_session_id="session-a",
        updated_at=(now - timedelta(days=31)).isoformat(),
    )
    registry.add(old)
    registry.purge(retention_days=30, now=now)  # first sweep of the (fake) day

    later_same_day = now + timedelta(hours=1)
    second = _closed_worker(
        "ccw-project-00000000-review-2",
        parent_session_id="session-a",
        updated_at=(later_same_day - timedelta(days=31)).isoformat(),
    )
    registry.add(second)

    throttled = registry.purge(retention_days=30, now=later_same_day)
    assert throttled.swept is False
    assert throttled.purged == []
    assert {record.name for record in registry.list()} == {second.name}

    forced = registry.purge(retention_days=30, now=later_same_day, force=True)
    assert forced.swept is True
    assert [record.name for record in forced.purged] == [second.name]


def test_purge_reseeds_the_high_water_mark_before_dropping_a_v2_session(
    tmp_path: Path,
) -> None:
    """140D89AB repro, reopened by retention's first delete path: a v2-era
    registry (no `ordinal_high_water_marks` entries at all) with 3 old
    CLOSED records under one session. Purging all 3 must not let
    `next_ordinal()` fall back to a record count of 0 -- the floor must be
    seeded to `3 + 1 = 4` before the records disappear, so a subsequent
    spawn under the same (e.g. resumed) session never reuses an
    already-spoken-for name."""
    registry_path = tmp_path / "registry.json"
    now = datetime(2026, 8, 30, tzinfo=UTC)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "workers": [
                    {
                        "name": f"ccw-project-00000000-review-{i}",
                        "repo_root": "/repo",
                        "worktree": "/repo",
                        "updated_at": (now - timedelta(days=31)).isoformat(),
                        "state": "closed",
                        "parent_session_id": "session-a",
                    }
                    for i in (1, 2, 3)
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = Registry(registry_path)

    result = registry.purge(retention_days=30, now=now)

    assert len(result.purged) == 3
    assert registry.list() == []
    assert registry.next_ordinal("session-a") == 4


def test_purge_with_nothing_to_purge_still_stamps_last_swept_at(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)

    result = registry.purge(retention_days=30, now=now)

    assert result.swept is True
    assert result.purged == []
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["last_swept_at"] == now.isoformat()

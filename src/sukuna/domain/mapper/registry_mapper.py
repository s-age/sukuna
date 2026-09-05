"""On-disk <-> `WorkerRecord` shape migration, and the Pydantic document
boundary (`RegistryDocument`/`WorkerRecordDocument`) that validates the
migrated shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from ...errors import RegistryDocumentError, SchemaVersionError
from ..entity.worker_record import WorkerRecord, WorkerState
from .command_mapper import _NAME_RE

# `RegistryDocument.ordinal_high_water_marks` and `last_swept_at` are new
# top-level fields with safe defaults
# (`{}` / `None`), so no `_migrate_legacy_fields()`-style rename/drop entry
# is needed for a v1/v2 document missing them -- pydantic's defaults handle
# absence the same way they already do for any other optional field. The
# version ceiling is bumped anyway so an older sukuna binary (which would
# silently ignore these fields and never advance the high-water-mark) never
# reads or overwrites a v3 registry (`parse_registry_document()`'s existing
# `schema_version > CURRENT_SCHEMA_VERSION` guard).
CURRENT_SCHEMA_VERSION = 3


class WorkerRecordDocument(BaseModel):
    """Pydantic validation boundary for one on-disk worker record, mirroring
    `WorkerRecord`'s current field set. `extra="forbid"` catches typos,
    hand-edits, and dropped-field leftovers early — the migration below is
    what makes this safe, by renaming or dropping every key that predates
    the current shape before this model ever sees the dict."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    # `min_length=1`: an empty string can only arrive via hand-edit -- every
    # regular write path takes `pane_ref`/`window_ref` from `SpawnResult`
    # (which already enforces `min_length=1`, terminal_mapper.py) and only
    # ever clears them to `None` (`detach_pane()`).
    repo_root: str = Field(min_length=1)
    worktree: str = Field(min_length=1)
    updated_at: str
    state: WorkerState
    pane_ref: str | None = Field(default=None, min_length=1)
    window_ref: str | None = Field(default=None, min_length=1)
    managed: bool = True
    parent_session_id: str | None = None
    goal: str | None = None
    parent_worker_name: str | None = None
    model: str | None = None
    # `min_length=1`, mirroring `pane_ref`/`window_ref` above: an empty
    # string can only arrive via hand-edit (`resolve_session_log_path()`
    # always returns either a real, non-empty path or `None`) and would
    # otherwise permanently latch `attach_session_log_path_if_unresolved()`'s
    # `is not None` guard (worker_record_session_log.py) into thinking this
    # worker's session_log_path is already resolved.
    session_log_path: str | None = Field(default=None, min_length=1)
    session_log_reset_at: str | None = None

    @field_validator("name")
    @classmethod
    def _name_matches_naming_convention(cls, value: str) -> str:
        """`name` is the registry's sole primary key and the `claude -n`/
        `SendMessage`/`--resume` session name (CLAUDE.md). `spawn` always
        produces a name satisfying `command_mapper.validate_name()`'s
        `_NAME_RE`; a hand-edited violation is caught here rather than
        surfacing later as a raw `ValidationError`."""
        if not _NAME_RE.fullmatch(value):
            raise ValueError(
                f"name must use lowercase letters, numbers, and hyphens: {value!r}"
            )
        return value

    @field_validator("updated_at")
    @classmethod
    def _updated_at_is_aware_isoformat(cls, value: str) -> str:
        """`utc_now()` always writes an aware ISO-8601 string. A hand-edited
        or externally-written value that is unparseable or naive would
        otherwise reach `worker_tree.py`'s `datetime.fromisoformat()` calls
        and `--since`/`--to` comparisons as a raw `ValueError`/`TypeError`
        instead of a `RegistryDocumentError`."""
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(
                f"updated_at is not a valid ISO-8601 string: {value!r}"
            ) from error
        if parsed.tzinfo is None:
            raise ValueError(f"updated_at must be timezone-aware: {value!r}")
        return value

    @field_validator("session_log_reset_at")
    @classmethod
    def _session_log_reset_at_is_aware_isoformat(cls, value: str | None) -> str | None:
        """Symmetric with `_updated_at_is_aware_isoformat()`, but `None` (no
        pending respawn reset) is valid -- unlike `updated_at`, which every
        record always has. A hand-edited or externally-written invalid value
        would otherwise reach `resolve_session_log_path()`'s
        `datetime.fromisoformat()` as a raw `ValueError`; the `mutate()`/
        `replace()` hook also guards this defensively (multiple layers of
        defense), but rejecting it at this boundary is the primary guard."""
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(
                f"session_log_reset_at is not a valid ISO-8601 string: {value!r}"
            ) from error
        if parsed.tzinfo is None:
            raise ValueError(f"session_log_reset_at must be timezone-aware: {value!r}")
        return value

    @field_validator("parent_worker_name")
    @classmethod
    def _parent_worker_name_matches_naming_convention(
        cls, value: str | None
    ) -> str | None:
        """ "no parent" already has exactly one spelling, `None` -- an empty
        string is only ever hand-edited in, never written by `spawn` (which
        pre-validates via `validate_name()`). Left to pass through, `""`
        would make `root_children()`/`build_tree()` (both
        `not parent_worker_name`) treat the record as a root while
        `children_of()`'s exact `==` match can never find it as anyone's
        child."""
        if value == "":
            raise ValueError("parent_worker_name must not be an empty string")
        if value is not None and not _NAME_RE.fullmatch(value):
            raise ValueError(
                "parent_worker_name must use lowercase letters, numbers, and "
                f"hyphens: {value!r}"
            )
        return value


class RegistryDocument(BaseModel):
    """Pydantic validation boundary for the whole registry.json shape."""

    model_config = ConfigDict(extra="forbid")

    # strict=True: on-disk `schema_version` must be a literal int, not a
    # numeric string -- pydantic's default lax mode would otherwise coerce
    # "3" into 3 and silently swallow a hand-edited registry.json's typo.
    schema_version: int = Field(strict=True)
    workers: list[WorkerRecordDocument]
    # `ordinal_high_water_marks`/`last_swept_at` are registry-wide state,
    # not per-worker, so these live here rather than on
    # `WorkerRecordDocument`. Keys are `spawn_placement.
    # ordinal_high_water_mark_key()` output (a real `parent_session_id`, or
    # the `"(none)"` sentinel); values only ever grow (`Registry.add()`'s
    # `claim_ordinal`, `retention.seed_ordinal_floors_before_purge()`).
    ordinal_high_water_marks: dict[str, int] = Field(default_factory=dict)
    last_swept_at: str | None = None

    @field_validator("ordinal_high_water_marks")
    @classmethod
    def _ordinal_high_water_marks_are_non_negative(
        cls, value: dict[str, int]
    ) -> dict[str, int]:
        """A hand-edited or corrupted negative floor would otherwise let
        `next_ordinal()`'s `max(record_based, persisted_floor)` silently
        favor the (still correct) record-based side forever, masking the
        corruption instead of surfacing it here."""
        for key, floor in value.items():
            if isinstance(floor, bool) or not isinstance(floor, int) or floor < 0:
                raise ValueError(
                    f"ordinal_high_water_marks[{key!r}] must be a non-negative "
                    f"integer: {floor!r}"
                )
        return value

    @field_validator("last_swept_at")
    @classmethod
    def _last_swept_at_is_aware_isoformat(cls, value: str | None) -> str | None:
        """Same shape as `WorkerRecordDocument`'s `_updated_at_is_aware_isoformat`/
        `_session_log_reset_at_is_aware_isoformat` -- `None` (never swept) is
        valid, unlike `updated_at`. An invalid value would otherwise reach
        `retention.sweep_is_due()`'s `datetime.fromisoformat()` as a raw
        `ValueError`."""
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(
                f"last_swept_at is not a valid ISO-8601 string: {value!r}"
            ) from error
        if parsed.tzinfo is None:
            raise ValueError(f"last_swept_at must be timezone-aware: {value!r}")
        return value

    @model_validator(mode="after")
    def _workers_have_unique_names(self) -> RegistryDocument:
        """`name` is the registry's sole primary key (CLAUDE.md); a
        duplicate would otherwise pass this boundary silently and get
        collapsed to one record (later-wins) by `records_from_document()`'s
        dict comprehension — a silent data loss this boundary exists to
        catch instead."""
        seen: set[str] = set()
        duplicates: set[str] = set()
        for item in self.workers:
            if item.name in seen:
                duplicates.add(item.name)
            seen.add(item.name)
        if duplicates:
            raise ValueError(
                f"duplicate worker name(s) in registry: {sorted(duplicates)}"
            )
        return self


def _migrate_legacy_fields(value: dict[str, Any]) -> dict[str, Any]:
    """Migrate an on-disk record shape to the current field set: renames
    `created_at`->`updated_at`, `peer_id`->`parent_session_id`, and
    `iterm_session_id`/`iterm_window_id`->`pane_ref`/`window_ref`; drops
    `role`/`worker_id`/`run_id`/`backend` with no replacement (CLAUDE.md)."""
    if not isinstance(value, dict):
        raise RegistryDocumentError(
            f"registry record must be a JSON object, got {type(value).__name__}"
        )
    value = dict(value)
    if "updated_at" not in value and "created_at" in value:
        value["updated_at"] = value.pop("created_at")
    if "parent_session_id" not in value and "peer_id" in value:
        value["parent_session_id"] = value.pop("peer_id")
    if "pane_ref" not in value and "iterm_session_id" in value:
        value["pane_ref"] = value.pop("iterm_session_id")
    if "window_ref" not in value and "iterm_window_id" in value:
        value["window_ref"] = value.pop("iterm_window_id")
    for obsolete_key in ("role", "worker_id", "run_id", "backend"):
        value.pop(obsolete_key, None)
    return value


def _document_from_record(record: WorkerRecord) -> WorkerRecordDocument:
    return WorkerRecordDocument(
        name=record.name,
        repo_root=record.repo_root,
        worktree=record.worktree,
        updated_at=record.updated_at,
        state=record.state,
        pane_ref=record.pane_ref,
        window_ref=record.window_ref,
        managed=record.managed,
        parent_session_id=record.parent_session_id,
        goal=record.goal,
        parent_worker_name=record.parent_worker_name,
        model=record.model,
        session_log_path=record.session_log_path,
        session_log_reset_at=record.session_log_reset_at,
    )


def _record_from_document(document: WorkerRecordDocument) -> WorkerRecord:
    return WorkerRecord(
        name=document.name,
        repo_root=document.repo_root,
        worktree=document.worktree,
        updated_at=document.updated_at,
        state=document.state,
        pane_ref=document.pane_ref,
        window_ref=document.window_ref,
        managed=document.managed,
        parent_session_id=document.parent_session_id,
        goal=document.goal,
        parent_worker_name=document.parent_worker_name,
        model=document.model,
        session_log_path=document.session_log_path,
        session_log_reset_at=document.session_log_reset_at,
    )


def parse_registry_document(raw: dict[str, Any]) -> RegistryDocument:
    """The on-disk registry.json dict's only entry point into the typed
    world: migrate every worker record's on-disk shape to the current one,
    enforce the schema-version ceiling, then validate the whole payload.
    A `PydanticValidationError` is translated into a
    `RegistryDocumentError` instead of ending in a traceback."""
    if not isinstance(raw, dict):
        raise RegistryDocumentError("registry.json root must be a JSON object")
    payload = dict(raw)
    payload.setdefault("schema_version", 1)
    schema_version = payload["schema_version"]
    if isinstance(schema_version, int) and schema_version > CURRENT_SCHEMA_VERSION:
        # Checked before the `workers` shape below: a newer schema version
        # may have legitimately restructured `workers`, and this sukuna
        # cannot judge that shape -- "update sukuna" must win over any
        # shape complaint about a document it doesn't understand yet.
        raise SchemaVersionError(
            f"registry schema_version {schema_version} is newer than this sukuna "
            f"understands (max {CURRENT_SCHEMA_VERSION}); update sukuna before using "
            "this registry"
        )
    workers = payload.get("workers", [])
    if not isinstance(workers, list):
        raise RegistryDocumentError("registry.json 'workers' must be a list")
    payload["workers"] = [_migrate_legacy_fields(item) for item in workers]
    try:
        return RegistryDocument.model_validate(payload)
    except PydanticValidationError as error:
        raise RegistryDocumentError(f"registry.json is malformed: {error}") from error


def records_from_document(document: RegistryDocument) -> dict[str, WorkerRecord]:
    return {item.name: _record_from_document(item) for item in document.workers}


def document_from_records(
    workers: dict[str, WorkerRecord], *, schema_version: int
) -> RegistryDocument:
    """The in-memory `WorkerRecord`s' entry point into the typed world
    before a write: no raw pydantic `ValidationError` leaks past this
    boundary. Runs before `Registry._write_unlocked()` opens its temp
    file, so a validation failure never leaves a stray `.registry-*` temp
    file behind."""
    try:
        return RegistryDocument(
            schema_version=schema_version,
            workers=[_document_from_record(worker) for worker in workers.values()],
        )
    except PydanticValidationError as error:
        raise RegistryDocumentError(
            f"cannot write registry: worker record is malformed: {error}"
        ) from error


@dataclass
class RegistryState:
    """In-memory, typed counterpart to `RegistryDocument` -- what
    `Registry._read_unlocked()`/`_write_unlocked()` (infrastructure/
    registry.py) actually operate on. Bundles the worker map with the
    registry-wide `ordinal_high_water_marks`/`last_swept_at` state so a
    single locked read/write round-trips all of it together."""

    workers: dict[str, WorkerRecord]
    ordinal_high_water_marks: dict[str, int]
    last_swept_at: str | None


def state_from_document(document: RegistryDocument) -> RegistryState:
    return RegistryState(
        workers=records_from_document(document),
        ordinal_high_water_marks=dict(document.ordinal_high_water_marks),
        last_swept_at=document.last_swept_at,
    )


def document_from_state(
    state: RegistryState, *, schema_version: int
) -> RegistryDocument:
    """Write-side counterpart to `state_from_document()`, mirroring
    `document_from_records()`'s pydantic-error translation (a hand-built
    `RegistryState` can carry the same kind of malformed `WorkerRecord`
    `document_from_records()` already guards against)."""
    workers_document = document_from_records(
        state.workers, schema_version=schema_version
    )
    try:
        return RegistryDocument(
            schema_version=schema_version,
            workers=workers_document.workers,
            ordinal_high_water_marks=dict(state.ordinal_high_water_marks),
            last_swept_at=state.last_swept_at,
        )
    except PydanticValidationError as error:
        raise RegistryDocumentError(
            f"cannot write registry: registry state is malformed: {error}"
        ) from error


def record_from_dict(value: dict[str, Any]) -> WorkerRecord:
    """Single-record convenience wrapper over the same migrate-then-validate
    path `parse_registry_document()` uses, for callers (and tests) working
    with one record's dict rather than the whole registry payload."""
    migrated = _migrate_legacy_fields(value)
    try:
        document = WorkerRecordDocument.model_validate(migrated)
    except PydanticValidationError as error:
        raise RegistryDocumentError(f"registry record is malformed: {error}") from error
    return _record_from_document(document)


def record_to_dict(record: WorkerRecord) -> dict[str, Any]:
    """Single-record convenience wrapper over `_document_from_record()`,
    mirroring `document_from_records()`'s write-side translation so this
    path cannot leak a raw pydantic `ValidationError` either."""
    try:
        document = _document_from_record(record)
    except PydanticValidationError as error:
        raise RegistryDocumentError(
            f"cannot serialize worker record: {error}"
        ) from error
    return document.model_dump(mode="json")

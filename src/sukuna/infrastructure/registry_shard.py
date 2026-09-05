"""Real I/O for the sharded worker registry: shard file discovery,
cross-shard summaries, and the one-time crash-safe migration from the
legacy single `registry.json` into `registry/<shard>.json` files. The
shard-key *decisions* live in `domain/service/shard_resolution.py`;
this module only reads/writes files and hands that module the data.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from ..domain.entity.worker_record import WorkerRecord, WorkerState
from ..domain.mapper.registry_mapper import RegistryState
from ..domain.service.session_log import encode_project_dir_name
from ..domain.service.shard_resolution import (
    CATCH_ALL_SHARD_KEY,
    MigrationRootInfo,
    ShardSummary,
    check_cross_shard_grafting,
    distribute_ordinal_high_water_marks,
    resolve_child_shard_key,
    resolve_migration_root,
    resolve_migration_shard_keys,
    resolve_root_shard_key,
)
from ..errors import NotFoundError, RegistryIOError, ValidationError
from .claude_projects import resolve_parent_session_log_path
from .registry import PurgeResult, Registry, sukuna_state_dir

__all__ = [
    "CATCH_ALL_SHARD_KEY",
    "MergedRegistryView",
    "all_shard_registries",
    "legacy_backup_path",
    "legacy_registry_path",
    "list_shard_paths",
    "migrate_if_needed",
    "read_all_shard_summaries",
    "registry_for_child_spawn",
    "registry_for_name",
    "registry_for_root_spawn",
    "shard_dir",
    "shard_path_for_key",
    "sukuna_state_dir",
]


def shard_dir() -> Path:
    return sukuna_state_dir() / "registry"


def legacy_registry_path() -> Path:
    return sukuna_state_dir() / "registry.json"


def legacy_backup_path() -> Path:
    legacy = legacy_registry_path()
    return legacy.parent / f"{legacy.name}.bak"


def shard_path_for_key(key: str) -> Path:
    return shard_dir() / f"{key}.json"


def list_shard_paths() -> list[Path]:
    """Every shard file, sorted by path -- deterministic first-match order
    for `domain/service/shard_resolution.py`'s reuse/name-lookup functions,
    which take the first match in whatever order they're handed."""
    directory = shard_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def _read_shard_summary(path: Path) -> ShardSummary:
    records = Registry(path).list()
    return ShardSummary(
        shard_key=path.stem,
        parent_session_ids=frozenset(record.parent_session_id for record in records),
        names=frozenset(record.name for record in records),
    )


def read_all_shard_summaries() -> list[ShardSummary]:
    return [_read_shard_summary(path) for path in list_shard_paths()]


def registry_for_root_spawn(*, parent_session_id: str | None, cwd: str) -> Registry:
    summaries = read_all_shard_summaries()
    key = resolve_root_shard_key(
        summaries,
        parent_session_id=parent_session_id,
        cwd_shard_key=encode_project_dir_name(cwd),
    )
    return Registry(shard_path_for_key(key))


def registry_for_child_spawn(
    *, parent_worker_name: str, parent_session_id: str | None, cwd: str
) -> Registry:
    summaries = read_all_shard_summaries()
    cwd_shard_key = encode_project_dir_name(cwd)
    check_cross_shard_grafting(
        summaries,
        parent_worker_name=parent_worker_name,
        parent_session_id=parent_session_id,
    )
    key = resolve_child_shard_key(
        summaries,
        parent_worker_name=parent_worker_name,
        parent_session_id=parent_session_id,
        cwd_shard_key=cwd_shard_key,
    )
    return Registry(shard_path_for_key(key))


def registry_for_name(name: str) -> Registry | None:
    """The shard already holding `name`, or `None` if no shard does."""
    for summary in read_all_shard_summaries():
        if name in summary.names:
            return Registry(shard_path_for_key(summary.shard_key))
    return None


def all_shard_registries() -> list[Registry]:
    return [Registry(path) for path in list_shard_paths()]


class MergedRegistryView(Registry):
    """Read-only fan-out of `.list()`/`.get()` across every shard, for the
    "every worker" views (`tree`, `inspect` with no `--worker`; `doctor`
    needs none). Every mutating method is overridden to raise
    `ValidationError`."""

    def __init__(self, shards: list[Registry]) -> None:
        super().__init__(None)
        self._shards = shards

    def list(self) -> list[WorkerRecord]:
        result: list[WorkerRecord] = []
        for shard in self._shards:
            result.extend(shard.list())
        return result

    def get(self, name: str) -> WorkerRecord:
        for shard in self._shards:
            try:
                return shard.get(name)
            except NotFoundError:
                continue
        raise NotFoundError(f"worker {name!r} is not registered")

    def _unsupported(self) -> NoReturn:
        raise ValidationError(
            "MergedRegistryView is read-only; resolve a single shard's "
            "Registry (registry_for_name()/registry_for_root_spawn()/"
            "registry_for_child_spawn()) to write"
        )

    def read_state(self) -> RegistryState:
        self._unsupported()

    def write_state(self, state: RegistryState) -> None:
        self._unsupported()

    def next_ordinal(self, parent_session_id: str | None) -> int:
        self._unsupported()

    def add(self, worker: WorkerRecord, *, claim_ordinal: int | None = None) -> None:
        self._unsupported()

    def replace(
        self, worker: WorkerRecord, *, expected_updated_at: str | None = None
    ) -> None:
        self._unsupported()

    def mutate(
        self,
        name: str,
        mutation: Callable[[WorkerRecord], None],
        *,
        expected_updated_at: str | None = None,
    ) -> WorkerRecord:
        self._unsupported()

    def transition(
        self,
        name: str,
        target: WorkerState,
        *,
        expected_updated_at: str | None = None,
    ) -> WorkerRecord:
        self._unsupported()

    def purge(
        self, *, retention_days: int, now: datetime, force: bool = False
    ) -> PurgeResult:
        self._unsupported()


# --- migration -------------------------------------------------------


def _is_committed(directory: Path) -> bool:
    """`registry/` counts as "migration already committed" only once it
    holds at least one shard file."""
    return directory.is_dir() and any(directory.glob("*.json"))


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _cleanup_stale_staging_dirs() -> None:
    """Remove leftover `.registry-migrating-<pid>` directories from a prior
    attempt that crashed before its commit `rename()`, but only once the
    owning pid is confirmed dead -- never delete a directory whose pid is
    still alive (it may be another process's migration in flight right
    now). A pid that has since been reused by an unrelated process is a
    false negative (skipped, left as clutter), never a false positive."""
    base = sukuna_state_dir()
    if not base.is_dir():
        return
    for candidate in base.glob(".registry-migrating-*"):
        if not candidate.is_dir():
            continue
        suffix = candidate.name.removeprefix(".registry-migrating-")
        try:
            pid = int(suffix)
        except ValueError:
            continue
        if not _pid_is_alive(pid):
            _rmtree(candidate)


def _resolve_transcript_shards(session_ids: set[str]) -> dict[str, str]:
    transcript_shard_by_session_id: dict[str, str] = {}
    for session_id in session_ids:
        transcript_path = resolve_parent_session_log_path(session_id)
        if transcript_path is not None:
            transcript_shard_by_session_id[session_id] = Path(
                transcript_path
            ).parent.name
    return transcript_shard_by_session_id


def _resolve_shard_keys(root_infos: dict[str, MigrationRootInfo]) -> dict[str, str]:
    transcript_shards = _resolve_transcript_shards(
        {
            info.parent_session_id
            for info in root_infos.values()
            if info.parent_session_id is not None
        }
    )
    return resolve_migration_shard_keys(
        root_infos, transcript_shard_by_session_id=transcript_shards
    )


def _bucket_records_by_shard(
    records: list[WorkerRecord], shard_key_by_name: dict[str, str]
) -> dict[str, dict[str, WorkerRecord]]:
    buckets: dict[str, dict[str, WorkerRecord]] = {}
    for record in records:
        buckets.setdefault(shard_key_by_name[record.name], {})[record.name] = record
    return buckets


def _reset_staging_dir(staging: Path) -> None:
    if staging.exists():
        _rmtree(staging)
    staging.mkdir(parents=True, exist_ok=False)


def _write_shard_states(
    staging: Path,
    buckets: dict[str, dict[str, WorkerRecord]],
    hwm_by_shard: dict[str, dict[str, int]],
    last_swept_at: str | None,
) -> None:
    for key, workers in buckets.items():
        shard_state = RegistryState(
            workers=workers,
            ordinal_high_water_marks=hwm_by_shard.get(key, {}),
            last_swept_at=last_swept_at,
        )
        Registry(staging / f"{key}.json").write_state(shard_state)


def _stage_shards(legacy: Path, staging: Path) -> None:
    state = Registry(legacy).read_state()
    records = list(state.workers.values())
    by_name = {record.name: record for record in records}
    root_infos = {
        record.name: resolve_migration_root(record, by_name) for record in records
    }
    shard_key_by_name = _resolve_shard_keys(root_infos)
    hwm_by_shard = distribute_ordinal_high_water_marks(
        state.ordinal_high_water_marks,
        shard_key_by_name=shard_key_by_name,
        records=records,
    )
    buckets = _bucket_records_by_shard(records, shard_key_by_name)
    _reset_staging_dir(staging)
    _write_shard_states(staging, buckets, hwm_by_shard, state.last_swept_at)


def _commit_staging(staging: Path, shards: Path) -> None:
    """The migration's sole commit point: one `rename()` from the staging
    directory onto `registry/`. A failure here (target already non-empty)
    means a concurrent migrator already won the race; this attempt adopts
    its result and discards its own now-redundant staging directory."""
    try:
        staging.rename(shards)
    except OSError:
        if _is_committed(shards):
            _rmtree(staging)
            return
        raise


def _finish_backup_rename(legacy: Path) -> None:
    backup = legacy_backup_path()
    try:
        legacy.rename(backup)
    except FileNotFoundError:
        pass  # already renamed by a concurrent process -- idempotent no-op


def _migrate_if_needed_unlocked() -> None:
    legacy = legacy_registry_path()
    if not legacy.exists():
        return
    shards = shard_dir()
    if _is_committed(shards):
        _finish_backup_rename(legacy)
        return
    _cleanup_stale_staging_dirs()
    staging = sukuna_state_dir() / f".registry-migrating-{os.getpid()}"
    _stage_shards(legacy, staging)
    _commit_staging(staging, shards)
    _finish_backup_rename(legacy)


def migrate_if_needed() -> None:
    """Trigger condition: the legacy `registry.json` still exists. Safe
    to call on every CLI invocation before touching any shard: a no-op
    once `registry.json` no longer exists.

    Every `Registry` read/write this triggers (`read_state()`/
    `write_state()`) already translates its own I/O failures into
    `RegistryIOError`. The raw `mkdir()`/`rename()`/`glob()` calls this
    module makes directly (staging directory creation, the commit
    rename, the `.bak` rename) do not -- wrapped here into the same
    `RegistryIOError`."""
    try:
        _migrate_if_needed_unlocked()
    except OSError as error:
        raise RegistryIOError(f"registry migration I/O failed: {error}") from error

"""Lock-protected on-disk worker registry."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..domain.entity.worker_record import WorkerRecord, WorkerState
from ..domain.mapper.registry_mapper import (
    CURRENT_SCHEMA_VERSION,
    RegistryState,
    document_from_state,
    parse_registry_document,
    state_from_document,
)
from ..domain.service.retention import seed_ordinal_floors_before_purge, sweep_is_due
from ..domain.service.retention import sweep_stale_records as _sweep_stale_records
from ..domain.service.spawn_placement import advance_ordinal_high_water_mark
from ..domain.service.spawn_placement import next_ordinal as _compute_next_ordinal
from ..domain.service.worker_record_session_log import (
    attach_session_log_path_if_unresolved,
)
from ..errors import (
    ConflictError,
    NotFoundError,
    RegistryDocumentError,
    RegistryIOError,
    StateError,
)
from .atomic_write import atomic_write
from .claude_projects import resolve_session_log_path

SessionLogResolver = Callable[[str, str, str | None], str | None]


@dataclass
class PurgeResult:
    """`Registry.purge()`'s return shape: the records it actually dropped
    (`[]` when the same-day throttle skipped the sweep -- see `swept`), and
    whether a sweep ran at all. `usecase/reconcile.py`'s `--prune` path and
    `usecase/spawn.py`'s automatic thinning both consume this."""

    purged: list[WorkerRecord]
    swept: bool


def sukuna_state_dir() -> Path:
    """Resolve sukuna's XDG-overridable state directory (holds
    `registry.json`, `setting.toml`, and `model_catalog.json`).
    `$XDG_STATE_HOME` wins when set; otherwise defaults to
    `~/Library/Application Support` on macOS and `~/.local/state` (the XDG
    base-directory convention) everywhere else."""
    default = (
        Path.home() / "Library" / "Application Support"
        if sys.platform == "darwin"
        else Path.home() / ".local" / "state"
    )
    state_home = Path(os.environ.get("XDG_STATE_HOME", str(default)))
    return state_home / "sukuna"


class Registry:
    def __init__(
        self,
        path: Path | None = None,
        *,
        session_log_resolver: SessionLogResolver = resolve_session_log_path,
    ) -> None:
        if path is None:
            path = sukuna_state_dir() / "registry.json"
        self.path = path
        self.lock_path = path.with_suffix(".lock")
        self._session_log_resolver = session_log_resolver

    def _attach_session_log_path(self, worker: WorkerRecord) -> None:
        """Best-effort, applied to `worker` in place just before every
        `mutate()`/`replace()` write. A resolve failure (missing project
        dir mid-scan, a hand-edited `session_log_reset_at` that fails
        `datetime.fromisoformat()`) never fails the state transition it's
        piggybacking on; the next transition retries."""
        try:
            attach_session_log_path_if_unresolved(worker, self._session_log_resolver)
        except (OSError, ValueError):
            pass

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Wraps every registry file operation (`mkdir`, lock, read,
        write). Any `OSError` here is converted to `RegistryIOError`, a
        `CrossBufferError` the batch-spawn loop can fail a single
        element on."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise RegistryIOError(f"registry I/O failed: {error}") from error

    def _read_unlocked(self) -> RegistryState:
        if not self.path.exists():
            return RegistryState(
                workers={}, ordinal_high_water_marks={}, last_swept_at=None
            )
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RegistryDocumentError(
                f"registry.json is not valid JSON: {error}"
            ) from error
        document = parse_registry_document(raw)
        return state_from_document(document)

    def _write_unlocked(self, state: RegistryState) -> None:
        document = document_from_state(state, schema_version=CURRENT_SCHEMA_VERSION)
        payload = document.model_dump(mode="json")
        content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        atomic_write(self.path, content, prefix=".registry-")

    def list(self) -> list[WorkerRecord]:
        with self._locked():
            return list(self._read_unlocked().workers.values())

    def read_state(self) -> RegistryState:
        """Full typed snapshot (workers + `ordinal_high_water_marks` +
        `last_swept_at`) under one locked read -- `list()` only exposes
        the worker map."""
        with self._locked():
            return self._read_unlocked()

    def write_state(self, state: RegistryState) -> None:
        """Unconditional overwrite of the whole file with `state` -- no
        existing-record checks, no `claim_ordinal` bookkeeping (unlike
        `add()`/`replace()`/`mutate()`)."""
        with self._locked():
            self._write_unlocked(state)

    def get(self, name: str) -> WorkerRecord:
        with self._locked():
            try:
                return self._read_unlocked().workers[name]
            except KeyError as error:
                raise NotFoundError(f"worker {name!r} is not registered") from error

    def next_ordinal(self, parent_session_id: str | None) -> int:
        """Naming ordinal for `worker_name()` -- the larger of the current
        record count and the persisted high-water-mark floor (see
        `domain/service/spawn_placement.next_ordinal()` for why both are
        needed). A single locked read of the same on-disk state `list()`
        would read; the caller (`usecase/spawn.py`) still re-reads via
        `list()` afterward for its own placement decision -- these are
        two independent snapshots of the same moment."""
        with self._locked():
            state = self._read_unlocked()
        return _compute_next_ordinal(
            list(state.workers.values()),
            state.ordinal_high_water_marks,
            parent_session_id,
        )

    def add(self, worker: WorkerRecord, *, claim_ordinal: int | None = None) -> None:
        """`claim_ordinal`, when given, atomically advances
        `ordinal_high_water_marks[key]` to `claim_ordinal + 1` in the
        same write as adding `worker`: the write either commits both or
        commits neither. Passed by `usecase/spawn.py` for every
        spawn-created worker; omitted (`None`) by every other caller."""
        with self._locked():
            state = self._read_unlocked()
            if worker.name in state.workers:
                raise StateError(f"worker {worker.name!r} already exists")
            state.workers[worker.name] = worker
            if claim_ordinal is not None:
                state.ordinal_high_water_marks = advance_ordinal_high_water_mark(
                    state.ordinal_high_water_marks,
                    parent_session_id=worker.parent_session_id,
                    claimed_ordinal=claim_ordinal,
                )
            self._write_unlocked(state)

    def replace(
        self, worker: WorkerRecord, *, expected_updated_at: str | None = None
    ) -> None:
        with self._locked():
            state = self._read_unlocked()
            current = state.workers.get(worker.name)
            if current is None:
                raise NotFoundError(f"worker {worker.name!r} is not registered")
            if (
                expected_updated_at is not None
                and current.updated_at != expected_updated_at
            ):
                raise ConflictError(
                    f"worker {worker.name!r} was modified concurrently "
                    f"(expected updated_at {expected_updated_at!r}, found {current.updated_at!r})"
                )
            self._attach_session_log_path(worker)
            state.workers[worker.name] = worker
            self._write_unlocked(state)

    def mutate(
        self,
        name: str,
        mutation: Callable[[WorkerRecord], None],
        *,
        expected_updated_at: str | None = None,
    ) -> WorkerRecord:
        """Lock, load, mutate one record, and persist atomically.

        `mutation` receives the loaded WorkerRecord and mutates it in place.
        If `mutation` raises, the exception propagates and NO write occurs
        (the lock is released with the on-disk state untouched).
        """
        with self._locked():
            state = self._read_unlocked()
            try:
                worker = state.workers[name]
            except KeyError as error:
                raise NotFoundError(f"worker {name!r} is not registered") from error
            if (
                expected_updated_at is not None
                and worker.updated_at != expected_updated_at
            ):
                raise ConflictError(
                    f"worker {name!r} was modified concurrently "
                    f"(expected updated_at {expected_updated_at!r}, found {worker.updated_at!r})"
                )
            mutation(worker)
            self._attach_session_log_path(worker)
            state.workers[name] = worker
            self._write_unlocked(state)
            return worker

    def transition(
        self,
        name: str,
        target: WorkerState,
        *,
        expected_updated_at: str | None = None,
    ) -> WorkerRecord:
        def _apply(worker: WorkerRecord) -> None:
            worker.transition_to(target)

        return self.mutate(name, _apply, expected_updated_at=expected_updated_at)

    def purge(
        self, *, retention_days: int, now: datetime, force: bool = False
    ) -> PurgeResult:
        """Retire CLOSED/FAILED(pane-less) records older than
        `retention_days` (`domain/service/retention.py`'s
        `is_retirement_candidate()`), the sole delete path in this class.
        Throttled to once per UTC calendar day (`sweep_is_due()`) unless
        `force=True` -- `usecase/reconcile.py`'s `--prune` passes
        `force=True` so a shortened `retention_days` takes effect
        immediately; `usecase/spawn.py`'s automatic thinning passes
        `force=False` and respects the throttle. Every affected
        `parent_session_id`'s ordinal high-water-mark is raised *before*
        its records are dropped (`seed_ordinal_floors_before_purge()`)."""
        with self._locked():
            state = self._read_unlocked()
            if not force and not sweep_is_due(state.last_swept_at, now=now):
                return PurgeResult(purged=[], swept=False)
            records = list(state.workers.values())
            _, survivors, purged = _sweep_stale_records(
                self.path, records, retention_days=retention_days, now=now
            )
            if purged:
                state.ordinal_high_water_marks = seed_ordinal_floors_before_purge(
                    state.ordinal_high_water_marks, records, purged
                )
                state.workers = {survivor.name: survivor for survivor in survivors}
            state.last_swept_at = now.isoformat()
            self._write_unlocked(state)
            return PurgeResult(purged=purged, swept=True)

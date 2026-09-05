"""Pure shard-key decision rules for the sharded registry: which shard a
new spawn writes to, which shard a name resolves to, and how a legacy
single-file registry's records map onto shards during migration. No
filesystem access here -- `infrastructure/registry_shard.py` reads and
hands in the summaries these functions need."""

from __future__ import annotations

from dataclasses import dataclass

from ...errors import ValidationError
from ..entity.worker_record import WorkerRecord
from .session_log import encode_project_dir_name
from .spawn_placement import ordinal_high_water_mark_key

# `encode_project_dir_name()` only ever emits `[A-Za-z0-9-]` (every
# non-alphanumeric input character becomes `-`), so a literal `(`/`)` here
# can never collide with a real cwd- or worktree-derived shard key --
# verified by `tests/test_domain_service_shard_resolution.py`'s
# non-collision check, per the card's own request.
CATCH_ALL_SHARD_KEY = "(legacy)"


@dataclass(frozen=True)
class ShardSummary:
    """One shard file's lightweight index: which `parent_session_id`s and
    `name`s it already holds. `infrastructure/registry_shard.py` builds
    this from each shard's own `Registry.list()`; the resolution functions
    below only ever read it, never the underlying records."""

    shard_key: str
    parent_session_ids: frozenset[str | None]
    names: frozenset[str]


def find_shard_for_parent_session(
    summaries: list[ShardSummary], parent_session_id: str | None
) -> str | None:
    """The first (in caller-supplied order -- the caller sorts for
    determinism) shard already holding a record spawned by
    `parent_session_id`, or `None` if no shard does yet."""
    for summary in summaries:
        if parent_session_id in summary.parent_session_ids:
            return summary.shard_key
    return None


def find_shard_for_name(summaries: list[ShardSummary], name: str) -> str | None:
    for summary in summaries:
        if name in summary.names:
            return summary.shard_key
    return None


def resolve_root_shard_key(
    summaries: list[ShardSummary],
    *,
    parent_session_id: str | None,
    cwd_shard_key: str,
) -> str:
    """A ROOT spawn's (no `parent_worker`) shard: reuse a shard already
    holding this session's records; only fall back to `cwd_shard_key` (the
    calling process's own cwd, `encode_project_dir_name()`-encoded) the
    first time this session ever spawns. Recomputing from `cwd` on every
    call would let a session spawning from more than one cwd split its own
    ordinal namespace across shards and re-mint an already-claimed
    ordinal."""
    existing = find_shard_for_parent_session(summaries, parent_session_id)
    return existing if existing is not None else cwd_shard_key


def resolve_child_shard_key(
    summaries: list[ShardSummary],
    *,
    parent_worker_name: str,
    parent_session_id: str | None,
    cwd_shard_key: str,
) -> str:
    """A nested spawn's (`parent_worker` given) shard: inherit the named
    parent worker's own shard, to any depth. Falls back to the ROOT rule
    above when `parent_worker_name` doesn't resolve to any known shard
    (the same "typo / already closed / never existed" fallback
    `usecase/spawn_shared.py`'s placement resolution already applies for
    anchor selection). Never itself rejects a resolvable parent in a
    different shard than the caller's own -- callers that must reject
    that (`infrastructure/registry_shard.py`'s
    `registry_for_child_spawn()`) call `check_cross_shard_grafting()`
    first."""
    found = find_shard_for_name(summaries, parent_worker_name)
    if found is not None:
        return found
    return resolve_root_shard_key(
        summaries, parent_session_id=parent_session_id, cwd_shard_key=cwd_shard_key
    )


def check_cross_shard_grafting(
    summaries: list[ShardSummary],
    *,
    parent_worker_name: str,
    parent_session_id: str | None,
) -> None:
    """Cross-shard grafting guard. Raises only when the calling session
    *already has records in some shard* (`find_shard_for_parent_session()`
    returns non-`None`) and that shard differs from the named
    `parent_worker`'s actual shard (`find_shard_for_name()`) --
    `worker_name()`'s token is derived deterministically from
    `parent_session_id` alone, so if this same session's records end up
    split across two shards, each shard's `next_ordinal()` only sees its
    own records and neither can detect the other already used an ordinal.
    No-op when either side is unresolvable: `parent_worker_name` not
    found (existing typo/closed/nonexistent fallback to the ROOT rule
    applies, no *other* shard to conflict with), or the calling session
    has no records anywhere yet -- a session with zero records has not
    "split" across shards; its first record will simply be created
    wherever this spawn lands, and reuse-first keeps every later spawn of
    the same session there too."""
    parent_shard = find_shard_for_name(summaries, parent_worker_name)
    if parent_shard is None:
        return
    own_shard = find_shard_for_parent_session(summaries, parent_session_id)
    if own_shard is None:
        return
    if parent_shard != own_shard:
        raise ValidationError(
            f"parent_worker '{parent_worker_name}' belongs to shard "
            f"'{parent_shard}', but this session's own shard is "
            f"'{own_shard}'; cross-shard grafting is rejected to prevent "
            "name/ordinal collisions across shard files"
        )


@dataclass(frozen=True)
class MigrationRootInfo:
    """The `(parent_session_id, worktree)` pair migration tiers off of --
    either a true ROOT record's own fields (`parent_worker_name` unset), or
    a pseudo-root's when the chain hits an orphan or a cycle before
    reaching one (see `resolve_migration_root()`)."""

    parent_session_id: str | None
    worktree: str


def resolve_migration_root(
    record: WorkerRecord, by_name: dict[str, WorkerRecord]
) -> MigrationRootInfo:
    """Walk `record`'s `parent_worker_name` chain up to its ROOT (no
    `parent_worker_name`), an orphan (names a worker not in `by_name`),
    or a cycle (continuing would revisit an already-walked name). Both
    stop at the last resolvable record, whose own fields are used as a
    convenience ROOT."""
    seen = {record.name}
    current = record
    while current.parent_worker_name:
        next_worker = by_name.get(current.parent_worker_name)
        if next_worker is None:
            break  # orphan: `current` is the pseudo-root
        if next_worker.name in seen:
            break  # cycle: `current` is the pseudo-root
        seen.add(next_worker.name)
        current = next_worker
    return MigrationRootInfo(
        parent_session_id=current.parent_session_id, worktree=current.worktree
    )


def migration_shard_key(
    root_info: MigrationRootInfo,
    *,
    transcript_shard_by_session_id: dict[str, str],
) -> str:
    """3-tier fallback: Tier 1, the root/pseudo-root's own session
    transcript directory (already `encode_project_dir_name()`-shaped, via
    `infrastructure.claude_projects.resolve_parent_session_log_path()`,
    looked up by the caller and handed in via
    `transcript_shard_by_session_id`); Tier 2,
    `encode_project_dir_name()` of the root/pseudo-root's own `worktree`,
    when Tier 1 has no entry; Tier 3, the catch-all shard when
    `parent_session_id` is `None`."""
    if root_info.parent_session_id is None:
        return CATCH_ALL_SHARD_KEY
    tier1 = transcript_shard_by_session_id.get(root_info.parent_session_id)
    if tier1 is not None:
        return tier1
    return encode_project_dir_name(root_info.worktree)


def resolve_migration_shard_keys(
    root_infos: dict[str, MigrationRootInfo],
    *,
    transcript_shard_by_session_id: dict[str, str],
) -> dict[str, str]:
    """One shard key per record name in `root_infos`, computed per
    *session* rather than per record: two roots sharing the same
    `parent_session_id` (a session that ROOT-spawned into more than one
    worktree, with a Tier 1 transcript miss) must resolve to the same
    shard, or `next_ordinal()`'s per-shard accounting for that session
    would split across files and re-mint an already-used ordinal. The
    `None`-session group needs no such grouping: `migration_shard_key()`
    always routes it to the catch-all regardless of `worktree` (Tier 3).
    Within a non-`None` group, the record-name-sorted-first member's
    fields represent the whole group for Tier 1/2 purposes -- deterministic,
    not dependent on dict iteration order. This also makes
    `distribute_ordinal_high_water_marks()`'s "first matching record"
    pick correct by construction."""
    by_session: dict[str, list[str]] = {}
    keys: dict[str, str] = {}
    for name, info in root_infos.items():
        if info.parent_session_id is None:
            keys[name] = migration_shard_key(
                info, transcript_shard_by_session_id=transcript_shard_by_session_id
            )
        else:
            by_session.setdefault(info.parent_session_id, []).append(name)

    for names in by_session.values():
        representative = root_infos[min(names)]
        shard_key = migration_shard_key(
            representative,
            transcript_shard_by_session_id=transcript_shard_by_session_id,
        )
        for name in names:
            keys[name] = shard_key
    return keys


@dataclass(frozen=True)
class DuplicateNameGroup:
    """One worker `name` found in more than one shard file -- the
    `reconcile` sweep, complementing spawn-time validation in
    `domain/service/spawn_name_uniqueness.py`. `entries` pairs each
    shard's own copy of the record with the key of the shard it came
    from, in the caller's `shard_records` order -- `usecase/
    registry_access.py` re-attaches each shard's file path for the JSON
    report."""

    name: str
    entries: tuple[tuple[str, WorkerRecord], ...]


def find_cross_shard_duplicate_names(
    shard_records: list[tuple[str, list[WorkerRecord]]],
) -> list[DuplicateNameGroup]:
    """Detection only -- callers must not use this to pick or remove a
    "winner" automatically: `name` is also a live `claude -n` session
    identifier, and no rule for choosing which copy to keep is safe in
    general. Report both; let a human decide."""
    entries_by_name: dict[str, list[tuple[str, WorkerRecord]]] = {}
    for shard_key, records in shard_records:
        for record in records:
            entries_by_name.setdefault(record.name, []).append((shard_key, record))
    return [
        DuplicateNameGroup(name=name, entries=tuple(entries))
        for name, entries in sorted(entries_by_name.items())
        if len(entries) > 1
    ]


def distribute_ordinal_high_water_marks(
    ordinal_high_water_marks: dict[str, int],
    *,
    shard_key_by_name: dict[str, str],
    records: list[WorkerRecord],
) -> dict[str, dict[str, int]]:
    """Split the legacy flat `ordinal_high_water_marks` dict across shards.
    Each key names a `parent_session_id` (or the `"(none)"` sentinel,
    `ordinal_high_water_mark_key()`); every record sharing that exact
    `parent_session_id` was spawned by the same session and already
    resolved (via `shard_key_by_name`) to the same shard -- the first
    match found determines it. A key with zero surviving records has no
    record left to derive a shard from and is dropped."""
    result: dict[str, dict[str, int]] = {}
    for key, floor in ordinal_high_water_marks.items():
        match = next(
            (
                record
                for record in records
                if ordinal_high_water_mark_key(record.parent_session_id) == key
            ),
            None,
        )
        if match is None:
            continue
        shard_key = shard_key_by_name[match.name]
        result.setdefault(shard_key, {})[key] = floor
    return result

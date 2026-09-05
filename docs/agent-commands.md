# `sukuna` — commands for agents

The entry point the orchestrator (a Claude Code session) uses to operate the lifecycle
of worker panes. Every command is non-interactive and prints exactly one JSON result to
stdout.

## Common behavior

### Global flags

| Flag | Description |
| --- | --- |
| `--registry <path>` | Use this path as the registry file instead of the default (for tests / isolation). When omitted, the usual sharded registry under the state directory is used. |

### Exit codes and error output

| Exit code | Meaning |
| --- | --- |
| `0` | Success |
| `2` | A sukuna-originated error (`VALIDATION_ERROR` / `WORKER_NOT_FOUND` / `INVALID_STATE` / `BACKEND_ERROR` / `CONFLICT` / `REGISTRY_IO_ERROR`, etc.) or an OS error. `{"ok": false, "error": "<code>", "message": "..."}` is printed to stdout. |
| `3` | `spawn` only. The batch was processed but at least one element failed (`failed` is non-empty). |
| `130` | Interrupted by Ctrl-C |

### Worker state transitions

`state` / `accept` / `close` / `respawn` all follow this transition table. A transition
not listed here is rejected with `INVALID_STATE`.

| Current state | Transitions allowed to |
| --- | --- |
| `starting` | `ready`, `failed`, `timed_out` |
| `ready` | `busy`, `failed`, `timed_out` |
| `busy` | `reported`, `failed`, `timed_out` |
| `reported` | `accepted`, `busy`, `failed`, `timed_out` |
| `accepted` | `closed`, `failed` |
| `failed` / `timed_out` / `closed` | `starting` (via `respawn` only) |

`spawn` registers the worker as `starting`, then moves it to `ready` once the pane is
created successfully, or to `failed` if pane creation fails.

### Worker names

`name` is the sole identifier: it is the registry's primary key, the value passed to
`--worker`, and the exact name used by `claude -n <name>` / `ListAgents` / `SendMessage`
/ `claude --resume <name>`. Its format is
`ccw-<repo-name-slug>-<session-id-token>-<role>-<ordinal>` (lowercase alphanumerics and
hyphens only). The ordinal is never reused within the same parent session.

### Registry record fields

Keys of the worker object returned by `spawn` / `inspect` / `state` / `accept` /
`close` / `respawn`.

| Key | Description |
| --- | --- |
| `name` | Worker name (primary key) |
| `repo_root` / `worktree` | Absolute paths resolved at spawn time |
| `state` | One of the states above |
| `updated_at` | Timestamp of the last state transition (UTC, ISO 8601) |
| `pane_ref` / `window_ref` | The backend's pane / window identifier. `%<digits>` for tmux, a UUID for iTerm2. Becomes `null` if the pane is lost. |
| `managed` | Whether sukuna is allowed to close this worker |
| `parent_session_id` | The `$CLAUDE_CODE_SESSION_ID` of the session that called spawn |
| `parent_worker_name` | The parent worker name given via `parent_worker` at spawn time (optional) |
| `goal` | A free-form description of purpose (optional, never passed to the `claude` command) |
| `model` | The `claude --model` value given at spawn time (optional) |
| `session_log_path` / `session_log_reset_at` | Where the Claude Code session transcript lives (internal use) |

## `sukuna spawn`

Creates worker panes from a JSON array on stdin. It takes no argv flags. Even a single
worker is passed as a one-element array. An empty array is rejected.

```text
echo '[{"role": "investigate", "repo": "/path/to/repo",
        "worktree": "/path/to/repo", "goal": "..."}]' | sukuna spawn
```

### Element keys

| Key | Required | Description |
| --- | --- | --- |
| `role` | Required | Lowercase-letter-first alphanumerics and hyphens (32 characters or fewer). Only embedded into the worker name; not stored in the registry. |
| `repo` | Required | Must be an existing directory |
| `worktree` | Required | Must be an existing directory. The worker launches `claude -n <name>` here. |
| `goal` | Optional | Free-form text, stored in the registry only |
| `parent_worker` | Optional | The calling worker's own name. Only format-validated; existence in the registry is not checked. Used for `sukuna-cli tree`'s multi-level nesting and for deciding pane placement's parent. |
| `active_pane_width` | Optional | Width (%, 1-99) when the new pane is created by an left/right split. Defaults to the `active_pane_width` setting when omitted. |
| `model` | Optional | Attached to the launch command as `claude --model <value>`. Uses the default model when omitted. |

Unknown keys are rejected.

### Preflight validation

If any of the following applies to even one element, the whole batch is rejected
without touching any pane (exit code `2`).

- An element is not an object, is missing a required key, or has an unknown key
- `repo` / `worktree` is empty or not an existing directory
- `role` / `parent_worker` has an invalid format
- `model` is empty or whitespace-only
- `active_pane_width` is out of range

### Automatic backend selection

If `$TMUX` is set, tmux is used; otherwise iTerm2 is used if `$ITERM_SESSION_ID` is
set. If both are set, the `preferred_backend` setting decides, defaulting to tmux when
unset. If neither is set, `VALIDATION_ERROR` is returned. A mismatch between an
existing worker's pane in the same parent session and the backend chosen this time also
yields `VALIDATION_ERROR`. Both checks run per element rather than in preflight, so the
affected element lands in `failed` (decided before any pane operation, leaving no trace
in the registry).

### `model` validation

Only when `$ANTHROPIC_API_KEY` is present in the caller's environment, the given
`model` is checked against Anthropic's models-list API. A local cache hit needs no
network call; a miss triggers at most one refetch per batch. If the model still isn't
found, or the refetch itself fails, that element becomes `failed`. Without
`$ANTHROPIC_API_KEY`, the value passes through unvalidated.

### Pane placement

Actual geometry is never read; placement is decided purely from the registry's
parent/child relationships. If parent X (`parent_worker`, or the orchestrator's own
pane when omitted) has no living children (a worker holding a pane and not `closed`),
X's pane is split left/right and the new pane placed on the right. If X has one or more
living children, the last living child's pane is split top/bottom and the new pane
placed below it. No new window is ever created. The anchor pane's existence is checked
right before the split; if the parent name resolves but its anchor pane is gone, there
is one fallback to directly under the orchestrator. If the orchestrator's own pane is
gone, spawn fails.

After spawn, if the same column has two or more members, their heights are equalized.
A failure to equalize or resize never fails the spawn itself; it is reported via
`equalize_warning` / `resize_warning` on the worker object.

### Launch command

A worker is launched as
`<login shell> -lc 'cd <worktree> && exec claude -n <name> [--model <model>]'`. Task
bodies are never included in the launch command (they are delivered as cross-session
messages).

### Automatic retention sweep

Every `spawn` call also deletes `closed` / `failed` (with no pane) records whose
`updated_at` is older than `retention_days` days. This is throttled to once per UTC
calendar day. Use `sukuna reconcile --prune` to run it immediately.

### Output

```json
{
  "succeeded": [{"index": 0, "worker": {...}}],
  "failed": [{"index": 1, "spec": {...}, "error": {"code": "...", "message": "..."},
              "worker_name": "..."}]
}
```

`index` is the element's position in the input array. Failures after preflight are
best-effort per element; the other elements still get spawned. `failed[].worker_name`
is present only when the worker was left in the registry as `failed`. The exit code is
`3` when `failed` is non-empty.

## `sukuna inspect [--worker <name>]`

Returns that worker's record when `--worker` is given, or every worker as
`{"workers": [...]}` when omitted. Never modifies the registry.

## `sukuna state --worker <name> --state <state>`

Applies a checked state transition.

- `--state closed` is rejected (use `sukuna close`).
- `--state starting` is rejected (use `sukuna respawn`).
- `--state failed` cannot be applied to a worker whose pane is still alive (or whose
  liveness can't be confirmed). If the pane is truly gone, use `sukuna reconcile`.
- `--state timed_out` while the pane is still alive returns with `pane_alive_warning`.
- After `--state busy` / `--state reported`, unless the caller is itself a nested
  worker, focus moves to the deepest `busy` worker's pane (or the caller's own pane if
  none). A failure here is reported via `focus_warning`; the transition itself still
  succeeds.

## `sukuna accept --worker <name>`

Moves a `reported` worker to `accepted`.

## `sukuna close --worker <name>`

Closes the pane of a worker that is `accepted`, `managed: true`, and holds a pane, then
marks it `closed`. Any other worker is rejected with `VALIDATION_ERROR`.

Also rejected if the worker has living children (workers whose `parent_worker_name`
points to it, that hold a pane, and are not `closed`); the offending children's names
are listed. When folding up a worker with grandchildren, close them in the order
grandchild → child → parent. If a child blocking the close actually has no live pane
anymore, run `sukuna reconcile` first.

After closing, if two or more members remain in the same column, their heights are
equalized. The output includes the worker record plus, as needed,
`close_warning` / `orphaned_children_warning` / `equalize_warning`.

## `sukuna respawn --worker <name>`

Reopens a `managed` worker in a terminal state (`failed` / `timed_out` / `closed`) that
has lost its pane, via `claude --resume <name>` in the same `worktree`. Placement rules
and output match `spawn`. `--model` is never attached (`--resume` restores the original
model).

## `sukuna capture --worker <name>`

Returns the worker pane's displayed content as
`{"ok": true, "exists": <bool>, "content": "..."}`. A worker with no pane yields
`VALIDATION_ERROR`.

## `sukuna reconcile [--prune]`

Reconciles the registry against the actual liveness of panes.

- A non-terminal-state worker holding a pane whose pane is actually gone is moved to
  `failed` and has `pane_ref` cleared (`reconciled`).
- A `failed` / `timed_out` worker holding a pane whose pane is actually gone keeps its
  state but has `pane_ref` cleared (`pane_cleared`).
- With `--prune`, `closed` / `failed` (with no pane) records older than
  `retention_days` are deleted immediately, bypassing the once-per-day throttle, and
  listed under `purged`.

Output is `{"reconciled": [...], "pane_cleared": [...]}` (plus `purged` when `--prune`
is given). All shards are covered when `--registry` is omitted.

## `sukuna focus [--worker <name>]`

Resizes a pane to the `active_pane_width` setting and moves the terminal's active pane.

- Without `--worker`, the caller's own pane is the target. If the calling session has
  no relation to sukuna (it has never spawned a worker, and its own pane isn't a living
  worker's pane), this is a no-op returning `{"ok": true, "skipped": "..."}`. Already
  being active also skips.
- With `--worker`, that worker's pane is the target. tmux's active pane is also
  switched only when the `should_focus_worker` setting is `true`.

Failures are returned as `resize_warning` / `select_pane_warning` / `is_active_warning`;
the command itself still succeeds. The Claude Code hook installable via
`sukuna-cli init` calls this command with no `--worker` on every `AskUserQuestion`.

## Orchestrator's standard procedure

1. Check reachable live sessions with `ListAgents`, and reuse a managed worker if one
   matches the same repository, worktree, and goal. Never match on name alone.
2. If there is no such worker, call `sukuna spawn` with the orchestrator's own pane
   selected. Batch multiple spawns into one call.
3. Take `succeeded[].worker.name` and confirm, with a bound on attempts, that the
   worker appears in `ListAgents`.
4. Send the bootstrap task via `SendMessage` only to workers that became reachable, and
   call `sukuna state --state busy`. Do the same when sending follow-up work to a
   `reported` worker.
5. On receiving a report, move it to `sukuna state --state reported`. Call
   `sukuna accept` only once the result and its verification are confirmed.
6. Call `sukuna close` only on `accepted` managed workers. Fold up in the order
   grandchild → child → parent.

Never close a worker that is `blocked` / `failed` / `timed_out`, or whose message was
held or refused; leave its pane in place for a human to inspect.

The bootstrap message should include the worker's name, the task scope and target
worktree, write permission and required verification, and the report format (at
minimum `STATUS` / `SUMMARY` / `CHANGED` / `VERIFY` / `HANDOFF`).

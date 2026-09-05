# `sukuna-cli` — commands for humans

The entry point humans use directly from a terminal. `init` is interactive, `tree`
prints text or runs the TUI, and `doctor` prints JSON.

## Common behavior

### Global flags

| Flag | Description |
| --- | --- |
| `--registry <path>` | Use this path as the registry file instead of the default (for tests / isolation) |

### State directory

The registry and settings file live under:

| Condition | Path |
| --- | --- |
| `$XDG_STATE_HOME` is set | `$XDG_STATE_HOME/sukuna` |
| macOS (unset) | `~/Library/Application Support/sukuna` |
| Other (unset) | `~/.local/state/sukuna` |

Files under it:

| File | Contents |
| --- | --- |
| `registry/<shard>.json` | Worker records (sharded) |
| `setting.toml` | Settings written by `sukuna-cli init` |
| `model_catalog.json` | Cache used to validate `spawn`'s `model` |

### Settings keys (`setting.toml`)

| Key | Type / range | Default | Purpose |
| --- | --- | --- | --- |
| `preferred_backend` | `"tmux"` / `"iterm2"` | unset (tmux preferred) | Tiebreaker when both `$TMUX` and `$ITERM_SESSION_ID` are set |
| `active_pane_width` | 1-99 | `50` | Pane width (%) applied on a left/right split in `spawn` and by `focus` |
| `should_focus_worker` | bool | `false` | Whether `sukuna focus --worker` also switches tmux's active pane |
| `tui_enabled` | bool | `false` | Whether `sukuna-cli tree` defaults to the interactive TUI |
| `retention_days` | positive integer | `30` | How many days to keep `closed` / `failed` (with no pane) records |

TOML has no `null`. An explicit `null` in the file is a parse error.

## `sukuna-cli init`

Interactive initial setup. Requires a TTY and takes no arguments. Every prompt can be
skipped with Enter, and sukuna works with default settings even if this is never run.
There are six questions.

| Item | Asked when | Written to |
| --- | --- | --- |
| Preferred backend | Only when both tmux and iTerm2 are installed. Skipped if only one is present; an error if neither is. | `preferred_backend` in `setting.toml` |
| Active pane width | Always | `active_pane_width` in `setting.toml` |
| Install the Claude Code hook | Always (`y/N`; shows "no change" if already installed) | `~/.claude/settings.json` |
| Switch panes on worker focus | Always (`y/n`; Enter keeps the existing setting) | `should_focus_worker` in `setting.toml` |
| Enable the tree TUI | Always (`Y/n`; Enter enables it) | `tui_enabled` in `setting.toml` |
| Registry retention (days) | Always | `retention_days` in `setting.toml` |

### The installed hook

Adds one entry to `PreToolUse` in `~/.claude/settings.json` that runs `sukuna focus`
(with no `--worker`) on every `AskUserQuestion`. This resizes the asking session's own
pane and moves the terminal's active pane to it; it does nothing if that pane is
already active. The hook command always exits `0` (fail-open), so a failure in
`sukuna focus` never blocks `AskUserQuestion`. Other existing settings and hooks are
preserved; only this one entry is merged in.

### Building the TUI bundle

When the TUI is enabled and npm is found, the package includes the TUI source, and no
usable bundle exists yet, the Node.js bundle is built on the spot. This requires
Node.js 24 or later and npm (installing them is left to the user). On failure, an error
is shown with the choice to retry or skip. Skipping still leaves `tui_enabled` on, and
`init` completes normally.

### Output

`{"ok": true, "settings_json_path": "..."}` plus the items written
(`installed_hooks` / `active_pane_width` / `preferred_backend` /
`should_focus_worker` / `tui_enabled` / `retention_days` / `setting_toml_path`).
Prompt text goes to stderr.

## `sukuna-cli tree`

Nests workers by `parent_worker_name` and groups roots by their spawning session
(`parent_session_id`).

| Flag | Description |
| --- | --- |
| `--limit <N>` | Maximum number of root groups to show (default 5), most-recently-updated member first. A shown group's members are never truncated. Ignored when the TUI starts. |
| `--since <datetime>` | Show only groups whose most-recently-updated member is at or after this local time (ISO 8601) |
| `--to <datetime>` | Show only groups whose most-recently-updated member is at or before this local time |
| `--resumable-only` | Keep only workers whose `worktree` is the current working directory (resumable via `claude --resume`) and their ancestors |
| `--tui` | Force the interactive TUI. Requires stdin/stdout to be a TTY, `tui_enabled` to be on, and Node.js 24 or later. Errors if any is unmet. |
| `--text` | Force plain text output |

`--tui` and `--text` are mutually exclusive. With neither given, the TUI starts
automatically when `tui_enabled` is on and the terminal/Node.js requirements are met,
and silently falls back to text otherwise.

### Text output

A box-drawn tree, one line per node. Each worker is shown as
`<name> (<updated_at in local time>)`, with `[resumable]` appended when applicable.
State and goal are not shown (use `sukuna inspect`). The group key is one of:

| Group key | Meaning |
| --- | --- |
| `<parent_session_id>` | A worker spawned from that session |
| `(unknown parent)` | A worker with no `parent_session_id` |
| `(orphaned: parent '<name>' not found)` | A worker whose `parent_worker_name` isn't in the registry |
| `(cycle: unreachable from any root)` | A cyclic reference unreachable from any root |

If `--limit` hides any groups, `(+N more parent groups)` is appended at the end.

`parent_session_id` alone only connects one level. If a worker spawns its own
grandchildren without passing its own name as `parent_worker`, those grandchildren show
up as a separate root group.

### TUI

An interactive TUI built with Ink/React. It reads the current tree that sukuna wrote to
a temporary JSON file; it never touches the network or writes to the registry. The
bundle is built by `sukuna-cli init`, or ahead of time via the repository's
`scripts/build_tui.sh`.

## `sukuna-cli doctor`

Returns the state of dependent commands and the bundle as JSON.

| Key | Meaning |
| --- | --- |
| `claude_cli` | Whether `claude` is on PATH |
| `it2run` / `it2run_available` | iTerm2's `it2run` helper's path and whether it runs |
| `tmux` / `tmux_available` | tmux's path and whether it's on PATH |
| `platform` | The value of `sys.platform` |
| `node_tui_available` | Whether Node.js 24 or later is found |
| `tui_bundle_source` | Where the TUI bundle came from (`"package"` / `"state_dir"` / `null`) |
| `tui_bundle_present` | Whether a usable TUI bundle exists |

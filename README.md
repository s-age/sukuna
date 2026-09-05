# sukuna

A pane manager CLI for named Claude Code workers. It lets Claude itself create, track,
and safely tear down the panes of a group of worker sessions that coordinate through
Claude Code's cross-session messaging (`ListAgents` / `SendMessage`).

Two backends are supported: tmux (macOS / Linux) and iTerm2 (macOS). The backend is
detected automatically from the environment (`$TMUX` / `$ITERM_SESSION_ID`). sukuna does
not implement message passing itself; that is the orchestrator skill's job. sukuna only
owns the pane lifecycle.

There are two entry points.

| Command | Audience | Output |
| --- | --- | --- |
| `sukuna` | Agents (the orchestrator) | JSON, non-interactive |
| `sukuna-cli` | Humans | Interactive / text |

## Demo

`sukuna-cli tree --tui` — a live view of worker sessions and their history:

![sukuna-cli tree --tui](docs/media/tree.png)

Spawning and managing workers, tmux backend:

https://github.com/user-attachments/assets/e5f47a97-38aa-4e4b-84f6-d3232ebb363c

Spawning and managing workers, iTerm2 backend: 

https://github.com/user-attachments/assets/d839e37b-58aa-4267-a85b-e29ef5fb7af2


## Installation

Requirements: the Claude Code CLI (`claude`) and Python 3.11 or later. Until the package
is published on PyPI, clone the repository and install it with pipx.

```text
git clone https://github.com/s-age/sukuna.git
cd sukuna
pipx install .
sukuna-cli init
```

`sukuna-cli init` is an interactive setup that lets you choose the preferred backend,
the pane width, whether to install the Claude Code hook, whether to enable the tree TUI,
and the registry retention period. Every prompt can be skipped, and sukuna works with
default settings even if `init` is never run.

To install directly without cloning:

```text
pipx install "git+https://github.com/s-age/sukuna.git"
```

The interactive TUI of `sukuna-cli tree` requires Node.js 24 or later and npm. When you
enable the TUI in `sukuna-cli init`, the bundle is built on the spot if npm is available.
If you `pip install .` from a clone, you may run `scripts/build_tui.sh` beforehand
instead. Everything else works without the TUI.

## Prerequisites

### tmux

- Install tmux (for example `brew install tmux`).
- The Claude Code session that calls sukuna must itself be running inside a tmux session
  (start `claude` inside `tmux new -s <name>`). sukuna cannot create a tmux session from
  the outside.

### iTerm2

- Enable the Python API in iTerm2's preferences.
- Install the iTerm2 Python runtime (Scripts → Manage → Install Python Runtime). The
  script bundled with sukuna runs inside iTerm2's own Python environment via `it2run`, so
  the `iterm2` package is not required on the sukuna side.
- On first run, macOS asks for permission to control iTerm2. Allow it.
- Script errors appear only in iTerm2's Script Console (Scripts → Manage → Console...).
  If `spawn` fails for no obvious reason, check there first.

`sukuna-cli doctor` reports whether `tmux`, `it2run`, `claude`, and Node.js are detected.

## Documentation

- [`sukuna` — commands for agents](docs/agent-commands.md)
- [`sukuna-cli` — commands for humans](docs/human-commands.md)

## Safety

- Task bodies are never embedded in the launch command; they are delivered as
  cross-session messages.
- Panes that are not in the registry, workers with `managed: false`, and workers in any
  state other than `accepted` are never closed.
- When a backend call fails, the pane is left in place and the worker is recorded as
  `failed`.
- The registry is updated with a lock and an atomic rename.

## License

[MIT](LICENSE)

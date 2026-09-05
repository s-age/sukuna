# sukuna instructions

- Keep the project macOS-specific. Backends live as independent modules (`terminal/iterm_backend.py`, `terminal/tmux_backend.py` as of 2026-08-20) sharing one `run(request) -> dict` interface (`spawn`/`close`, keys `pane_ref`/`window_ref`/`anchor_pane_ref`); adding a new terminal emulator means adding a backend module this way, not special-casing `cli.py`.
- `sukuna` manages panes (iTerm2 and tmux) and the local registry only. Claude Code cross-session messaging remains the calling Skill's responsibility.
- Preserve the invariant that only a `managed` worker in `accepted` state may be closed.
- Keep runtime dependencies minimal. The iTerm2 Python API remains optional because the main CLI is testable without iTerm2; the tmux backend only requires the `tmux` binary, not a Python binding.
- Add focused pytest coverage for every registry state-transition and close-safety change.
- `repository/mapping.py`'s `record_to_dict()`/`record_from_dict()` round-trip is not forward-compatible: renaming or adding a field breaks any on-disk registry written by an older version (`record_from_dict()` does a bare `cls(**value)`). See `CLAUDE.md` for the renames this already forced and how legacy keys are read back in `record_from_dict()`.

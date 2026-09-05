"""Business rule for whether `sukuna-cli tree` (no `--tui`/`--text` flag)
auto-launches the interactive TUI instead of printing static text.
`tui_enabled` is the gate switch: opted-in environments get the TUI by
default, everyone else falls back to text. Infrastructure gathers the
four inputs; this function only decides, with no side effects."""

from __future__ import annotations


def should_launch_tui(
    *,
    tui_enabled: bool,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
    node_and_bundle_ok: bool,
) -> bool:
    return tui_enabled and stdin_is_tty and stdout_is_tty and node_and_bundle_ok

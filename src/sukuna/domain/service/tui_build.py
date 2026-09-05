"""Business rule for whether `sukuna-cli init` should attempt to build the
TUI bundle from the embedded `tui/` source. Infrastructure gathers the
four inputs; this function only decides, with no side effects -- the
same shape as `tree_mode.should_launch_tui()`.

`bundle_already_present` is included so an idempotent `init` re-run does
not rebuild every time: a usable bundle already existing is itself a
reason *not* to attempt a build, so it is the one input whose satisfied
(gate-passing) value is `False` rather than `True`."""

from __future__ import annotations


def should_attempt_tui_build(
    *,
    tui_enabled_answer: bool,
    npm_available: bool,
    tui_source_available: bool,
    bundle_already_present: bool,
) -> bool:
    return (
        tui_enabled_answer
        and npm_available
        and tui_source_available
        and not bundle_already_present
    )

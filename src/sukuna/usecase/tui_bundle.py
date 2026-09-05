"""Single place that decides which TUI bundle candidate (if any) is usable:
the package-embedded bundle, or a version-stamped one built into the
user's state dir by `sukuna-cli init`. `doctor.py`, `tree.py` (auto-launch
gating and launch-time bundle resolution), and `init.py` (build-attempt
gating) all read this instead of duplicating the priority order."""

from __future__ import annotations

from typing import Literal

from ..domain.service.tui_bundle import stamp_matches_source
from ..infrastructure import npm_runtime
from ..infrastructure.tui import launcher as tui_launcher


def state_dir_bundle_usable() -> bool:
    return tui_launcher.state_dir_bundle_exists() and stamp_matches_source(
        tui_launcher.state_dir_stamp_fingerprint(),
        npm_runtime.embedded_tui_source_fingerprint(),
    )


def tui_bundle_source() -> Literal["package", "state_dir"] | None:
    if tui_launcher.bundle_present():
        return "package"
    if state_dir_bundle_usable():
        return "state_dir"
    return None


def tui_bundle_available() -> bool:
    return tui_bundle_source() is not None

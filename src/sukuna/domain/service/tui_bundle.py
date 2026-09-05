"""Business rule for whether a state-dir TUI bundle's version stamp still
matches the currently embedded `tui/` source. Pure comparison, no I/O --
`infrastructure/tui/launcher.py` reads/writes the stamp and bundle bytes,
`usecase/tui_bundle.py` calls this to decide whether the on-disk pair is
trustworthy."""

from __future__ import annotations


def stamp_matches_source(
    stamped_fingerprint: str | None, current_fingerprint: str | None
) -> bool:
    if stamped_fingerprint is None or current_fingerprint is None:
        return False
    return stamped_fingerprint == current_fingerprint

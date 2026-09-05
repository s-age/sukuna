"""Check required local executables."""

from __future__ import annotations

import shutil
import sys
from typing import Any

from ..infrastructure import node_runtime
from ..infrastructure.terminal.iterm_backend import Iterm2Backend
from ..infrastructure.terminal.tmux_backend import TmuxBackend
from . import tui_bundle


def doctor() -> dict[str, Any]:
    source = tui_bundle.tui_bundle_source()
    return {
        "claude_cli": shutil.which("claude") is not None,
        **Iterm2Backend().diagnostics(),
        **TmuxBackend().diagnostics(),
        "platform": sys.platform,
        "node_tui_available": node_runtime.node_satisfies_tui_minimum(),
        "tui_bundle_source": source,
        "tui_bundle_present": source is not None,
    }

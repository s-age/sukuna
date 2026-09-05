"""API-connectivity smoke test for the tmux backend: confirms our `tmux`
CLI invocations actually reach a real tmux binary and round-trip a
spawn -> close cycle. It asserts only that no operation raises — behavioral
correctness (split direction, layout reconstruction, error conversion, ...)
is the responsibility of the mocked tests in test_tmux_backend.py, not this
file. This is the one place in the suite that talks to real tmux."""

import shutil
import subprocess
from collections.abc import Iterator

import pytest

from sukuna.infrastructure.terminal.tmux_backend import TmuxBackend

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is not installed"
)


@pytest.fixture()
def anchor_pane() -> Iterator[str]:
    """A scratch tmux pane to split against, in its own window so it never
    touches whatever pane pytest itself is running in."""
    created = subprocess.run(
        ["tmux", "new-window", "-d", "-P", "-F", "#{pane_id}"],
        check=True,
        capture_output=True,
        text=True,
    )
    pane_id = created.stdout.strip()
    yield pane_id
    subprocess.run(
        ["tmux", "kill-pane", "-t", pane_id], check=False, capture_output=True
    )


def test_spawn_and_close_round_trip_against_real_tmux(anchor_pane: str) -> None:
    backend = TmuxBackend()

    spawned = backend.run(
        {
            "operation": "spawn",
            "command": "exec /bin/zsh",
            "anchor_pane_ref": anchor_pane,
            "split_direction": "horizontal",
        }
    )
    assert spawned["ok"] is True

    backend.run({"operation": "close", "pane_ref": spawned["pane_ref"]})

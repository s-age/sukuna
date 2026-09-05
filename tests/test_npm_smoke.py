"""Real-binary smoke test for npm detection, same spirit as
test_tmux_smoke.py: confirms `npm_runtime`'s `shutil.which("npm")`-based
detection reaches an actual npm binary when one is installed. It does not
run `npm ci`/`npm run build` -- those stay covered by the mocked tests in
test_infrastructure_npm_runtime.py."""

import shutil

import pytest

from sukuna.infrastructure import npm_runtime

pytestmark = pytest.mark.skipif(
    shutil.which("npm") is None, reason="npm is not installed"
)


def test_npm_executable_and_availability_reach_a_real_npm_binary() -> None:
    assert npm_runtime.npm_executable() is not None
    assert npm_runtime.npm_available() is True

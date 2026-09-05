"""Thin launcher for the bundled iTerm2 Python API script."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ...errors import BackendError
from . import resolver

_SCRIPT = Path(__file__).with_name("iterm_script.py")


class Iterm2Backend:
    """Run one iTerm2 Python API operation through iTerm2's `it2run` helper."""

    def __init__(self, *, it2run: Path | None = None, timeout: float = 20) -> None:
        self.it2run = it2run or Path(
            os.environ.get("SUKUNA_IT2RUN", str(resolver.default_it2run_path()))
        )
        self.timeout = timeout

    def available(self) -> bool:
        return self.it2run.is_file() and os.access(self.it2run, os.X_OK)

    def diagnostics(self) -> dict[str, Any]:
        return {"it2run": str(self.it2run), "it2run_available": self.available()}

    def _invoke_it2run(
        self, request_path: Path, *, deadline: float
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [str(self.it2run), str(_SCRIPT), str(request_path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except subprocess.TimeoutExpired as error:
            raise BackendError("iTerm2 operation timed out") from error

    @staticmethod
    def _no_result_detail(completed: subprocess.CompletedProcess[str]) -> str:
        return (
            completed.stderr.strip() or completed.stdout.strip() or "no result returned"
        )

    def _await_result_file(
        self,
        result_path: Path,
        *,
        deadline: float,
        completed: subprocess.CompletedProcess[str],
    ) -> None:
        # `it2run` hands the script off to iTerm2's own process and returns
        # before that script has actually run, so the result file is not
        # necessarily present yet — poll for it within what remains of the
        # deadline above instead of checking exactly once.
        while not result_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not result_path.exists():
            raise BackendError(
                f"iTerm2 operation failed: {self._no_result_detail(completed)}"
            )

    @staticmethod
    def _parse_result_file(result_path: Path) -> dict[str, Any]:
        raw = result_path.read_text(encoding="utf-8")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as error:
            raise BackendError(
                f"iTerm2 result file is not valid JSON: {error}"
            ) from error
        if not isinstance(result, dict):
            raise BackendError(
                f"iTerm2 result file must be a JSON object, got {type(result).__name__}"
            )
        if not result.get("ok"):
            raise BackendError(result.get("error", "iTerm2 operation failed"))
        return result

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.available():
            raise BackendError(
                f"iTerm2 launcher not found or not executable: {self.it2run}"
            )
        with tempfile.TemporaryDirectory(prefix="sukuna-") as directory:
            request_path = Path(directory) / "request.json"
            result_path = Path(directory) / "result.json"
            request = dict(request)
            request["result_path"] = str(result_path)
            request_path.write_text(json.dumps(request), encoding="utf-8")
            # The whole operation — the `it2run` call plus the result-file
            # poll below — shares one `self.timeout` budget. The deadline is
            # computed here, before `subprocess.run`, so that a slow `it2run`
            # return eats into the poll's remaining time instead of each step
            # getting its own full `self.timeout`.
            deadline = time.monotonic() + self.timeout
            completed = self._invoke_it2run(request_path, deadline=deadline)
            if completed.returncode != 0:
                # A non-zero exit means `it2run` failed to hand the script off
                # to iTerm2 at all — unlike its normal immediate exit 0, no
                # result file will ever appear, so fail fast instead of
                # polling out the rest of the deadline.
                raise BackendError(
                    f"iTerm2 operation failed: {self._no_result_detail(completed)}"
                )
            self._await_result_file(result_path, deadline=deadline, completed=completed)
            return self._parse_result_file(result_path)

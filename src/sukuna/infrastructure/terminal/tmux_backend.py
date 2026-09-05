"""tmux pane management backend.

Unlike the iTerm2 backend, `tmux` subcommands run and return synchronously —
there is no out-of-band script host, no notification-lag race between a
successful split and the pane becoming visible, and no hidden GUI dialog
that can block the next call. `split-window`/`new-window` are asked to
print the new pane/window id directly (`-P -F`), so the result is read
straight from stdout with no polling.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from ...errors import BackendError

_TMUX = "tmux"
_PANE_FORMAT = "#{pane_id}|#{window_id}"


class TmuxBackend:
    """Run one tmux pane operation through the `tmux` CLI."""

    def __init__(self, *, tmux: str = _TMUX, timeout: float = 10) -> None:
        self.tmux = tmux
        self.timeout = timeout

    def available(self) -> bool:
        return shutil.which(self.tmux) is not None

    def diagnostics(self) -> dict[str, Any]:
        return {"tmux": self.tmux, "tmux_available": self.available()}

    def _run(self, *args: str) -> str:
        if not self.available():
            raise BackendError(f"tmux executable not found on PATH: {self.tmux}")
        try:
            completed = subprocess.run(
                [self.tmux, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise BackendError("tmux operation timed out") from error
        if completed.returncode != 0:
            detail = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or "tmux exited non-zero"
            )
            raise BackendError(f"tmux operation failed: {detail}")
        return completed.stdout.strip()

    def _run_int(self, *args: str) -> int:
        """`_run()` variant for subcommands whose stdout must be a single
        integer (`display-message -p` numeric formats). A non-numeric
        stdout is translated to `BackendError` here, same discipline as
        `_run()`'s returncode/timeout translation."""
        output = self._run(*args)
        try:
            return int(output)
        except ValueError as error:
            raise BackendError(
                f"unexpected display-message output: {output!r}"
            ) from error

    def _pane_exists(self, pane_ref: str) -> bool:
        panes = self._run("list-panes", "-a", "-F", "#{pane_id}").splitlines()
        return pane_ref in panes

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request["operation"]
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "spawn": self._spawn,
            "close": self._close,
            "verify": self._verify,
            "capture": self._capture,
            "pane_heights": self._pane_heights,
            "set_pane_height": self._set_pane_height,
            "resize": self._resize,
            "resize_context": self._resize_context,
            "select_pane": self._select_pane,
            "is_active": self._is_active,
        }
        handler = handlers.get(operation)
        if handler is None:
            raise BackendError(f"unknown operation: {operation}")
        return handler(request)

    def _spawn(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request["command"]
        anchor = request.get("anchor_pane_ref")
        if not anchor:
            raise BackendError("worker layout anchor does not exist")
        if not self._pane_exists(anchor):
            raise BackendError(f"worker layout anchor does not exist: {anchor}")

        split_flag = "-h" if request["split_direction"] == "horizontal" else "-v"
        args = [
            "split-window",
            split_flag,
            "-t",
            anchor,
            "-P",
            "-F",
            _PANE_FORMAT,
            command,
        ]
        output = self._run(*args)
        # Same translation discipline as `_run_int()`: a returncode-0
        # `split-window` whose stdout doesn't carry the requested
        # `#{pane_id}|#{window_id}` shape must surface as `BackendError`,
        # not a raw unpack `ValueError`.
        pane_ref, sep, window_ref = output.rpartition("|")
        if not sep or not pane_ref or not window_ref:
            raise BackendError(f"unexpected split-window output: {output!r}")
        # `active_pane_width` application is not this backend's concern:
        # this stays a plain split, and `usecase.spawn` issues its own
        # follow-up `resize()` (via `infrastructure.terminal.operations`,
        # which assembles the raw `resize_context` + `resize` calls around
        # the clamping policy in `domain.service.pane_width`) -- a RAW_IO
        # backend must not import domain (`tests/test_infrastructure_boundary.py`).
        return {"ok": True, "pane_ref": pane_ref, "window_ref": window_ref}

    def _resize(self, request: dict[str, Any]) -> dict[str, Any]:
        """Raw, unclamped `resize-pane -x <percent>%` (window-wide). Any
        safety clamping happens upstream, in
        `infrastructure.terminal.operations`, before `percent` ever
        reaches here."""
        pane_ref = request["pane_ref"]
        if not self._pane_exists(pane_ref):
            raise BackendError(f"resize target pane does not exist: {pane_ref}")
        self._run("resize-pane", "-t", pane_ref, "-x", f"{request['percent']}%")
        return {"ok": True}

    def _resize_context(self, request: dict[str, Any]) -> dict[str, Any]:
        """Raw state a caller needs to decide whether/how to clamp a
        `resize()` of `pane_ref`: its window's width, and how many other
        panes share that window."""
        pane_ref = request["pane_ref"]
        if not self._pane_exists(pane_ref):
            raise BackendError(f"resize target pane does not exist: {pane_ref}")
        window_ref = self._run("display-message", "-p", "-t", pane_ref, "#{window_id}")
        window_width = self._run_int(
            "display-message", "-p", "-t", pane_ref, "#{window_width}"
        )
        other_pane_count = len(
            [
                ref
                for ref in self._run(
                    "list-panes", "-t", window_ref, "-F", "#{pane_id}"
                ).splitlines()
                if ref != pane_ref
            ]
        )
        return {
            "ok": True,
            "window_width": window_width,
            "other_pane_count": other_pane_count,
        }

    def _close(self, request: dict[str, Any]) -> dict[str, Any]:
        pane_ref = request["pane_ref"]
        if not self._pane_exists(pane_ref):
            raise BackendError("tmux worker pane does not exist")
        self._run("kill-pane", "-t", pane_ref)
        return {"ok": True}

    def _verify(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "exists": self._pane_exists(request["pane_ref"])}

    def _select_pane(self, request: dict[str, Any]) -> dict[str, Any]:
        pane_ref = request["pane_ref"]
        if not self._pane_exists(pane_ref):
            raise BackendError(f"select-pane target pane does not exist: {pane_ref}")
        self._run("select-pane", "-t", pane_ref)
        return {"ok": True}

    def _is_active(self, request: dict[str, Any]) -> dict[str, Any]:
        """Active-pane determination: considered active only when both
        `#{pane_active}` and `#{window_active}` are "1" -- `session_attached`
        is deliberately not consulted. This can misjudge a detached tmux
        session's last active pane as active, but since nobody is watching
        during detachment anyway, the only real-world effect is not being
        focused."""
        pane_ref = request["pane_ref"]
        if not self._pane_exists(pane_ref):
            raise BackendError(f"is_active target pane does not exist: {pane_ref}")
        output = self._run(
            "display-message", "-p", "-t", pane_ref, "#{pane_active},#{window_active}"
        )
        # Same translation discipline as `_run_int()`: a returncode-0
        # `display-message` whose stdout doesn't carry the requested
        # "pane_active,window_active" shape must surface as `BackendError`,
        # not a raw unpack `ValueError`.
        parts = output.split(",")
        if len(parts) != 2:
            raise BackendError(f"unexpected display-message output: {output!r}")
        pane_active, window_active = parts
        return {"ok": True, "active": pane_active == "1" and window_active == "1"}

    def _capture(self, request: dict[str, Any]) -> dict[str, Any]:
        pane_ref = request["pane_ref"]
        if not self._pane_exists(pane_ref):
            return {"ok": True, "exists": False, "content": None}
        content = self._run("capture-pane", "-p", "-t", pane_ref)
        return {"ok": True, "exists": True, "content": content}

    def _pane_heights(self, request: dict[str, Any]) -> dict[str, Any]:
        """Raw heights read -- the query half of a two-step `equalize`
        operation."""
        column = request["column_pane_refs"]
        for pane_ref in column:
            if not self._pane_exists(pane_ref):
                raise BackendError(f"equalize target pane does not exist: {pane_ref}")
        heights = [
            self._run_int("display-message", "-p", "-t", ref, "#{pane_height}")
            for ref in column
        ]
        return {"ok": True, "heights": heights}

    def _set_pane_height(self, request: dict[str, Any]) -> dict[str, Any]:
        """Raw, unconditional `resize-pane -y`. No existence recheck:
        `operations.py`'s assembly always issues `pane_heights` (which
        checks every member) immediately before these writes."""
        self._run(
            "resize-pane", "-t", request["pane_ref"], "-y", str(request["height"])
        )
        return {"ok": True}

import shutil
import subprocess
from collections.abc import Callable
from typing import ClassVar, cast

import pytest

from sukuna.errors import BackendError
from sukuna.infrastructure.terminal.tmux_backend import TmuxBackend

_PANE_FORMAT = "#{pane_id}|#{window_id}"


@pytest.fixture(autouse=True)
def _fake_tmux_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`TmuxBackend._run()` gates every call on `available()`, which calls
    `shutil.which("tmux")` — a real filesystem/PATH lookup independent of
    `subprocess.run`. Mocking only `subprocess.run` would leave these tests
    still dependent on a real tmux binary being installed; patch `which` too
    so this whole module runs with no tmux on PATH (unlike the smoke test in
    test_tmux_smoke.py, which deliberately exercises the real binary)."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _FakeTmux:
    """Stateful stand-in for the `tmux` CLI: tracks which pane/window refs
    currently 'exist' and answers the handful of subcommands `TmuxBackend`
    issues, mirroring real tmux's call sequencing (a `list-panes` existence
    check before each mutating operation) without spawning a process."""

    def __init__(
        self,
        panes: dict[str, str] | None = None,
        heights: dict[str, int] | None = None,
        window_widths: dict[str, int] | None = None,
        active: dict[str, str] | None = None,
    ) -> None:
        self.panes: dict[str, str] = dict(panes or {})  # pane_ref -> window_ref
        self.captures: dict[str, str] = {}
        self.heights: dict[str, int] = dict(heights or {})  # pane_ref -> #{pane_height}
        self.window_widths: dict[str, int] = dict(
            window_widths or {}
        )  # window_ref -> #{window_width}
        # pane_ref -> raw "#{pane_active},#{window_active}" stdout
        self.active: dict[str, str] = dict(active or {})
        self.session_name = "fake-session"
        self._next_pane = 100
        self._next_window = 10
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        assert args[0] == "tmux"
        sub = args[1]
        handler = self._SUBCOMMANDS.get(sub)
        if handler is None:
            raise AssertionError(f"unexpected tmux subcommand: {sub}")
        return handler(self, args)

    def _handle_list_panes(self, args: list[str]) -> subprocess.CompletedProcess:
        if "-t" in args:
            window_ref = args[args.index("-t") + 1]
            pane_ids = [
                pane for pane, window in self.panes.items() if window == window_ref
            ]
            return _completed(stdout="\n".join(pane_ids))
        return _completed(stdout="\n".join(self.panes))

    def _handle_split_window(self, args: list[str]) -> subprocess.CompletedProcess:
        anchor = args[args.index("-t") + 1]
        window_ref = self.panes[anchor]
        new_pane = self._allocate_pane(window_ref)
        return _completed(stdout=f"{new_pane}|{window_ref}")

    def _handle_new_window(self, args: list[str]) -> subprocess.CompletedProcess:
        new_window = f"@{self._next_window}"
        self._next_window += 1
        new_pane = self._allocate_pane(new_window)
        return _completed(stdout=f"{new_pane}|{new_window}")

    def _handle_kill_pane(self, args: list[str]) -> subprocess.CompletedProcess:
        pane_ref = args[args.index("-t") + 1]
        self.panes.pop(pane_ref, None)
        self.captures.pop(pane_ref, None)
        return _completed()

    def _handle_capture_pane(self, args: list[str]) -> subprocess.CompletedProcess:
        pane_ref = args[args.index("-t") + 1]
        return _completed(stdout=self.captures.get(pane_ref, ""))

    def _handle_display_message(self, args: list[str]) -> subprocess.CompletedProcess:
        # tmux's format string is always the trailing positional argument
        # (`-p [-t target] format`), regardless of where `-t` sits.
        fmt = args[-1]
        target = args[args.index("-t") + 1] if "-t" in args else None
        if fmt == "#{session_name}":
            return _completed(stdout=self.session_name)
        if fmt == "#{window_id}":
            return _completed(stdout=self.panes[cast(str, target)])
        if fmt == "#{pane_height}":
            return _completed(stdout=str(self.heights[cast(str, target)]))
        if fmt == "#{window_width}":
            window_ref = self.panes[cast(str, target)]
            return _completed(stdout=str(self.window_widths.get(window_ref, 200)))
        if fmt == "#{pane_active},#{window_active}":
            return _completed(stdout=self.active[cast(str, target)])
        raise AssertionError(f"unexpected display-message format: {fmt}")

    def _handle_resize_pane(self, args: list[str]) -> subprocess.CompletedProcess:
        target = args[args.index("-t") + 1]
        if "-y" in args:
            self.heights[target] = int(args[args.index("-y") + 1])
        elif "-x" in args:
            pass  # recorded in self.calls; no simulated width-tracking needed by any test
        else:
            raise AssertionError(f"resize-pane call missing -x/-y: {args}")
        return _completed()

    def _handle_select_pane(self, args: list[str]) -> subprocess.CompletedProcess:
        return _completed()

    _SUBCOMMANDS: ClassVar[
        dict[str, Callable[["_FakeTmux", list[str]], subprocess.CompletedProcess]]
    ] = {
        "list-panes": _handle_list_panes,
        "split-window": _handle_split_window,
        "new-window": _handle_new_window,
        "kill-pane": _handle_kill_pane,
        "capture-pane": _handle_capture_pane,
        "display-message": _handle_display_message,
        "resize-pane": _handle_resize_pane,
        "select-pane": _handle_select_pane,
    }

    def _allocate_pane(self, window_ref: str) -> str:
        new_pane = f"%{self._next_pane}"
        self._next_pane += 1
        self.panes[new_pane] = window_ref
        return new_pane


def test_spawn_splits_the_anchor_pane_and_returns_new_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(panes={"%1": "@1"})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    result = backend.run(
        {
            "operation": "spawn",
            "command": "exec /bin/zsh",
            "anchor_pane_ref": "%1",
            "split_direction": "horizontal",
        }
    )

    assert result["ok"] is True
    assert result["pane_ref"] != "%1"
    assert result["pane_ref"]
    assert result["window_ref"] == "@1"
    split_call = next(call for call in fake.calls if call[1] == "split-window")
    assert split_call[2] == "-h"
    assert split_call[split_call.index("-t") + 1] == "%1"


def test_spawn_never_issues_a_resize_call_even_when_active_pane_width_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`active_pane_width` application lives in
    `infrastructure.terminal.operations` (which issues its own
    `resize_context` + `resize` calls, using the clamping policy in
    `domain.service.pane_width` -- covered by
    tests/test_terminal_operations.py), not this RAW_IO backend.
    `_spawn()` ignores the key entirely; a stray one in the request must
    not trigger an inline resize here."""
    fake = _FakeTmux(panes={"%1": "@1"})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    result = backend.run(
        {
            "operation": "spawn",
            "command": "exec /bin/zsh",
            "anchor_pane_ref": "%1",
            "split_direction": "horizontal",
            "active_pane_width": 70,
        }
    )

    split_call = next(call for call in fake.calls if call[1] == "split-window")
    assert "-p" not in split_call
    assert not any(call[1] == "resize-pane" for call in fake.calls)
    assert "resize_warning" not in result


def test_spawn_rejects_a_missing_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTmux(panes={})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    with pytest.raises(BackendError):
        backend.run(
            {
                "operation": "spawn",
                "command": "exec /bin/zsh",
                "anchor_pane_ref": "%999999",
                "split_direction": "horizontal",
            }
        )


def test_close_rejects_a_missing_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTmux(panes={})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    with pytest.raises(BackendError):
        backend.run({"operation": "close", "pane_ref": "%999999"})


def test_close_kills_a_live_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTmux(panes={"%1": "@1"})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()
    spawned = backend.run(
        {
            "operation": "spawn",
            "command": "exec /bin/zsh",
            "anchor_pane_ref": "%1",
            "split_direction": "horizontal",
        }
    )

    backend.run({"operation": "close", "pane_ref": spawned["pane_ref"]})

    assert not backend._pane_exists(spawned["pane_ref"])


def test_verify_reports_existence_across_a_spawn_and_close_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(panes={"%1": "@1"})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()
    spawned = backend.run(
        {
            "operation": "spawn",
            "command": "exec /bin/zsh",
            "anchor_pane_ref": "%1",
            "split_direction": "horizontal",
        }
    )

    present = backend.run({"operation": "verify", "pane_ref": spawned["pane_ref"]})
    assert present == {"ok": True, "exists": True}

    backend.run({"operation": "close", "pane_ref": spawned["pane_ref"]})

    absent = backend.run({"operation": "verify", "pane_ref": spawned["pane_ref"]})
    assert absent == {"ok": True, "exists": False}


def test_capture_reports_content_across_a_spawn_and_close_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(panes={"%1": "@1"})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()
    spawned = backend.run(
        {
            "operation": "spawn",
            "command": "exec /bin/zsh",
            "anchor_pane_ref": "%1",
            "split_direction": "horizontal",
        }
    )
    fake.captures[spawned["pane_ref"]] = "line one\nline two"

    present = backend.run({"operation": "capture", "pane_ref": spawned["pane_ref"]})
    assert present == {"ok": True, "exists": True, "content": "line one\nline two"}

    backend.run({"operation": "close", "pane_ref": spawned["pane_ref"]})

    absent = backend.run({"operation": "capture", "pane_ref": spawned["pane_ref"]})
    assert absent == {"ok": True, "exists": False, "content": None}


def test_run_converts_a_nonzero_returncode_into_a_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args, **kwargs):
        return _completed(returncode=1, stderr="no server running")

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = TmuxBackend()

    with pytest.raises(BackendError, match="no server running"):
        backend._run("list-panes", "-a", "-F", "#{pane_id}")


def test_run_converts_a_timeout_into_a_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 10))

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = TmuxBackend()

    with pytest.raises(BackendError, match="timed out"):
        backend._run("list-panes", "-a", "-F", "#{pane_id}")


def test_pane_heights_reads_every_column_members_height_without_resizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw `pane_heights` read and per-pane `set_pane_height` writes
    are pinned here; the average-height/last-member-excluded behavior is
    pinned at the assembly layer, in `tests/test_terminal_operations.py`,
    over the same [10, 20, 30] scenario."""
    fake = _FakeTmux(
        panes={"%1": "@1", "%2": "@1", "%3": "@1"},
        heights={"%1": 10, "%2": 20, "%3": 30},
    )
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    result = backend.run(
        {"operation": "pane_heights", "column_pane_refs": ["%1", "%2", "%3"]}
    )

    assert result == {"ok": True, "heights": [10, 20, 30]}
    assert not any(call[1] == "resize-pane" for call in fake.calls)


def test_pane_heights_rejects_a_missing_column_member_without_reading_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(panes={"%1": "@1"}, heights={"%1": 10})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    with pytest.raises(BackendError, match="equalize target pane does not exist"):
        backend.run(
            {"operation": "pane_heights", "column_pane_refs": ["%1", "%999999"]}
        )

    assert not any(call[1] == "resize-pane" for call in fake.calls)


def test_set_pane_height_issues_the_raw_resize_pane_y(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(panes={"%1": "@1"}, heights={"%1": 10})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    result = backend.run(
        {"operation": "set_pane_height", "pane_ref": "%1", "height": 20}
    )

    assert result == {"ok": True}
    resize_calls = [call for call in fake.calls if call[1] == "resize-pane"]
    assert len(resize_calls) == 1
    assert resize_calls[0][resize_calls[0].index("-t") + 1] == "%1"
    assert resize_calls[0][resize_calls[0].index("-y") + 1] == "20"


def test_resize_applies_the_requested_percent_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is a raw, unclamped primitive -- whatever `percent` it's given
    is exactly what reaches `resize-pane -x`, with no window-width/sibling
    lookups at all. The clamping policy itself is covered by
    tests/test_domain_service_pane_width.py and
    tests/test_terminal_operations.py (which drives this backend's
    `resize_context` + `resize` together, the way `operations.py` does in
    production)."""
    fake = _FakeTmux(panes={"%1": "@1"})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    result = backend.run({"operation": "resize", "pane_ref": "%1", "percent": 90})

    assert result == {"ok": True}
    resize_call = next(call for call in fake.calls if call[1] == "resize-pane")
    assert resize_call[resize_call.index("-t") + 1] == "%1"
    assert resize_call[resize_call.index("-x") + 1] == "90%"
    assert not any(call[1] == "display-message" for call in fake.calls)
    assert not any(call[1] == "list-panes" and "-a" not in call for call in fake.calls)


def test_resize_rejects_a_missing_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTmux(panes={})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    with pytest.raises(BackendError, match="resize target pane does not exist"):
        backend.run({"operation": "resize", "pane_ref": "%999999", "percent": 70})

    assert not any(call[1] == "resize-pane" for call in fake.calls)


def test_resize_context_reports_the_window_width_and_other_pane_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(
        panes={"%1": "@1", "%2": "@1", "%3": "@1"}, window_widths={"@1": 200}
    )
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    result = backend.run({"operation": "resize_context", "pane_ref": "%1"})

    assert result == {"ok": True, "window_width": 200, "other_pane_count": 2}


def test_resize_context_reports_no_other_panes_for_a_lone_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(panes={"%1": "@1"}, window_widths={"@1": 200})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    result = backend.run({"operation": "resize_context", "pane_ref": "%1"})

    assert result == {"ok": True, "window_width": 200, "other_pane_count": 0}


def test_resize_context_rejects_a_missing_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTmux(panes={})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    with pytest.raises(BackendError, match="resize target pane does not exist"):
        backend.run({"operation": "resize_context", "pane_ref": "%999999"})


def test_select_pane_switches_the_active_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTmux(panes={"%1": "@1"})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    result = backend.run({"operation": "select_pane", "pane_ref": "%1"})

    assert result == {"ok": True}
    select_call = next(call for call in fake.calls if call[1] == "select-pane")
    assert select_call[select_call.index("-t") + 1] == "%1"


def test_select_pane_rejects_a_missing_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTmux(panes={})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    with pytest.raises(BackendError, match="select-pane target pane does not exist"):
        backend.run({"operation": "select_pane", "pane_ref": "%999999"})

    assert not any(call[1] == "select-pane" for call in fake.calls)


def _corrupting(fake: _FakeTmux, *, subcommand: str, fmt: str | None, stdout: str):
    """Delegate to `fake` except for one subcommand (optionally narrowed to
    one `display-message` format string), which returns returncode 0 with a
    corrupted stdout — the exit-0-but-malformed-output shape a misbehaving
    tmux (or a wrapper script around it) can produce. Everything
    else (the pre-parse `list-panes` existence checks in particular) stays
    healthy so these tests exercise exactly the parse site."""

    def run(args: list[str], **kwargs):
        if args[1] == subcommand and (fmt is None or args[-1] == fmt):
            return _completed(stdout=stdout)
        return fake(args, **kwargs)

    return run


def test_spawn_converts_a_malformed_split_window_stdout_into_a_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A returncode-0 `split-window` whose stdout lacks the requested
    `#{pane_id}|#{window_id}` shape must surface as `BackendError` — not
    escape as a raw unpack `ValueError` past `usecase.spawn`'s
    `CrossBufferError` handling, which would skip the FAILED compensation,
    abort the rest of the batch, and end in a bare traceback."""
    fake = _FakeTmux(panes={"%1": "@1"})
    monkeypatch.setattr(
        subprocess,
        "run",
        _corrupting(
            fake, subcommand="split-window", fmt=None, stdout="malformed-no-pipe\n"
        ),
    )
    backend = TmuxBackend()

    with pytest.raises(BackendError, match="unexpected split-window output"):
        backend.run(
            {
                "operation": "spawn",
                "command": "exec /bin/zsh",
                "anchor_pane_ref": "%1",
                "split_direction": "horizontal",
            }
        )


def test_resize_context_converts_a_non_numeric_window_width_into_a_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(panes={"%1": "@1"})
    monkeypatch.setattr(
        subprocess,
        "run",
        _corrupting(
            fake,
            subcommand="display-message",
            fmt="#{window_width}",
            stdout="not-a-number\n",
        ),
    )
    backend = TmuxBackend()

    with pytest.raises(BackendError, match="unexpected display-message output"):
        backend.run({"operation": "resize_context", "pane_ref": "%1"})


def test_pane_heights_converts_a_non_numeric_pane_height_into_a_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(panes={"%1": "@1"}, heights={"%1": 10})
    monkeypatch.setattr(
        subprocess,
        "run",
        _corrupting(
            fake,
            subcommand="display-message",
            fmt="#{pane_height}",
            stdout="not-a-number\n",
        ),
    )
    backend = TmuxBackend()

    with pytest.raises(BackendError, match="unexpected display-message output"):
        backend.run({"operation": "pane_heights", "column_pane_refs": ["%1"]})


def test_is_active_reports_true_when_pane_and_window_are_both_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(panes={"%1": "@1"}, active={"%1": "1,1"})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    result = backend.run({"operation": "is_active", "pane_ref": "%1"})

    assert result == {"ok": True, "active": True}


@pytest.mark.parametrize("raw", ["0,1", "1,0", "0,0"])
def test_is_active_reports_false_unless_both_pane_and_window_are_active(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """`session_attached` is deliberately not consulted -- only
    `#{pane_active}` AND `#{window_active}`."""
    fake = _FakeTmux(panes={"%1": "@1"}, active={"%1": raw})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    result = backend.run({"operation": "is_active", "pane_ref": "%1"})

    assert result == {"ok": True, "active": False}


def test_is_active_converts_a_malformed_display_message_output_into_a_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux(panes={"%1": "@1"})
    monkeypatch.setattr(
        subprocess,
        "run",
        _corrupting(
            fake,
            subcommand="display-message",
            fmt="#{pane_active},#{window_active}",
            stdout="not-the-expected-shape\n",
        ),
    )
    backend = TmuxBackend()

    with pytest.raises(BackendError, match="unexpected display-message output"):
        backend.run({"operation": "is_active", "pane_ref": "%1"})


def test_is_active_rejects_a_missing_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTmux(panes={})
    monkeypatch.setattr(subprocess, "run", fake)
    backend = TmuxBackend()

    with pytest.raises(BackendError, match="is_active target pane does not exist"):
        backend.run({"operation": "is_active", "pane_ref": "%999999"})

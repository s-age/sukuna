import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from sukuna.errors import BackendError
from sukuna.infrastructure.terminal import resolver
from sukuna.infrastructure.terminal.iterm_backend import Iterm2Backend


@pytest.fixture()
def fake_it2run(tmp_path: Path) -> Path:
    """A stand-in for iTerm2's `it2run` launcher: `available()` only checks
    that the path exists and is executable, so an empty executable file is
    enough — the real work happens in the monkeypatched `subprocess.run`."""
    script = tmp_path / "it2run"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_default_it2run_is_derived_from_resolver_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No explicit `it2run=` and no `$SUKUNA_IT2RUN`: the default must come
    from `resolver.default_it2run_path()` rather than a hardcoded
    `/Applications` path, so it stays in sync with `iterm2_installed()`."""
    monkeypatch.delenv("SUKUNA_IT2RUN", raising=False)
    home_bundle = tmp_path / "home" / "iTerm.app"
    home_bundle.mkdir(parents=True)
    monkeypatch.setattr(
        resolver, "_ITERM_BUNDLE_PATHS", (tmp_path / "missing.app", home_bundle)
    )

    backend = Iterm2Backend()

    assert backend.it2run == home_bundle / "Contents" / "Resources" / "it2run"


def test_sukuna_it2run_env_var_still_overrides_the_derived_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "custom" / "it2run"
    monkeypatch.setenv("SUKUNA_IT2RUN", str(override))

    backend = Iterm2Backend()

    assert backend.it2run == override


def test_spawn_forwards_the_placement_contract_and_returns_new_refs(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    seen_request = {}

    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        seen_request.update(request)
        result_path = Path(request["result_path"])
        result_path.write_text(
            json.dumps(
                {"ok": True, "pane_ref": "new-session-id", "window_ref": "tab-1"}
            ),
            encoding="utf-8",
        )
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    result = backend.run(
        {
            "operation": "spawn",
            "command": "exec /bin/zsh",
            "anchor_pane_ref": "anchor-session-id",
            "split_direction": "horizontal",
        }
    )

    assert seen_request["anchor_pane_ref"] == "anchor-session-id"
    assert seen_request["split_direction"] == "horizontal"
    assert result == {"ok": True, "pane_ref": "new-session-id", "window_ref": "tab-1"}


def test_spawn_forwards_active_pane_width_when_given(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    seen_request = {}

    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        seen_request.update(request)
        result_path = Path(request["result_path"])
        result_path.write_text(
            json.dumps(
                {"ok": True, "pane_ref": "new-session-id", "window_ref": "tab-1"}
            ),
            encoding="utf-8",
        )
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    backend.run(
        {
            "operation": "spawn",
            "command": "exec /bin/zsh",
            "anchor_pane_ref": "anchor-session-id",
            "split_direction": "horizontal",
            "active_pane_width": 70,
        }
    )

    assert seen_request["active_pane_width"] == 70


def test_spawn_still_raises_when_the_operation_itself_fails(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(request["result_path"])
        result_path.write_text(
            json.dumps({"ok": False, "error": "worker layout anchor does not exist"}),
            encoding="utf-8",
        )
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    with pytest.raises(BackendError):
        backend.run(
            {
                "operation": "spawn",
                "command": "exec /bin/zsh",
                "anchor_pane_ref": "missing-session-id",
                "split_direction": "horizontal",
            }
        )


def test_verify_reports_exists_true_when_the_session_is_found(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(request["result_path"])
        result_path.write_text(
            json.dumps({"ok": True, "exists": True}), encoding="utf-8"
        )
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    result = backend.run({"operation": "verify", "pane_ref": "some-session-id"})

    assert result == {"ok": True, "exists": True}


def test_verify_reports_exists_false_without_failing_the_operation(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(request["result_path"])
        result_path.write_text(
            json.dumps({"ok": True, "exists": False}), encoding="utf-8"
        )
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    result = backend.run({"operation": "verify", "pane_ref": "missing-session-id"})

    assert result == {"ok": True, "exists": False}


def test_verify_still_raises_when_the_operation_itself_fails(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(request["result_path"])
        result_path.write_text(
            json.dumps({"ok": False, "error": "iTerm2 API unavailable"}),
            encoding="utf-8",
        )
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    with pytest.raises(BackendError):
        backend.run({"operation": "verify", "pane_ref": "some-session-id"})


def test_select_pane_forwards_the_pane_ref_and_returns_ok(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    seen_request = {}

    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        seen_request.update(request)
        result_path = Path(request["result_path"])
        result_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    result = backend.run({"operation": "select_pane", "pane_ref": "some-session-id"})

    assert seen_request["pane_ref"] == "some-session-id"
    assert result == {"ok": True}


def test_select_pane_still_raises_when_the_operation_itself_fails(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(request["result_path"])
        result_path.write_text(
            json.dumps(
                {"ok": False, "error": "select_pane target session does not exist"}
            ),
            encoding="utf-8",
        )
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    with pytest.raises(BackendError):
        backend.run({"operation": "select_pane", "pane_ref": "missing-session-id"})


def test_capture_reports_content_when_the_session_is_found(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(request["result_path"])
        result_path.write_text(
            json.dumps({"ok": True, "exists": True, "content": "line one\nline two"}),
            encoding="utf-8",
        )
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    result = backend.run({"operation": "capture", "pane_ref": "some-session-id"})

    assert result == {"ok": True, "exists": True, "content": "line one\nline two"}


def test_capture_reports_exists_false_without_failing_the_operation(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(request["result_path"])
        result_path.write_text(
            json.dumps({"ok": True, "exists": False, "content": None}), encoding="utf-8"
        )
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    result = backend.run({"operation": "capture", "pane_ref": "missing-session-id"})

    assert result == {"ok": True, "exists": False, "content": None}


def test_capture_still_raises_when_the_operation_itself_fails(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(request["result_path"])
        result_path.write_text(
            json.dumps({"ok": False, "error": "iTerm2 API unavailable"}),
            encoding="utf-8",
        )
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    with pytest.raises(BackendError):
        backend.run({"operation": "capture", "pane_ref": "some-session-id"})


def test_run_raises_backend_error_when_result_file_is_not_valid_json(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    """Confirms the fallback behavior when result.json's content is
    malformed (e.g. from disk corruption), independent of any race
    (`write_result()` is already atomic, so this content itself can no
    longer arise from a race)."""

    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(request["result_path"])
        result_path.write_text(
            '{"ok": true, "pane_ref": ', encoding="utf-8"
        )  # partial write
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    with pytest.raises(BackendError):
        backend.run({"operation": "verify", "pane_ref": "some-session-id"})


def test_run_raises_backend_error_when_result_file_is_empty(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    """Confirms the fallback behavior when result.json is empty,
    independent of any race (`write_result()` is already atomic, so this
    content itself can no longer arise from a race)."""

    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(request["result_path"])
        result_path.write_text("", encoding="utf-8")
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    with pytest.raises(BackendError):
        backend.run({"operation": "verify", "pane_ref": "some-session-id"})


def test_result_poll_shares_the_same_timeout_budget_as_the_subprocess_call(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    """`self.timeout` covers the whole operation (it2run call + result poll),
    not `self.timeout` for each step separately. With a
    monkeypatched clock: it2run is made to consume 90% of the budget before
    returning (without writing the result file), and the subsequent poll
    loop must then only be allowed to run for the remaining 10% — total
    elapsed time must stay within `self.timeout`, not stretch to ~2x it."""

    clock = [0.0]

    def fake_monotonic() -> float:
        return clock[0]

    def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(time, "sleep", fake_sleep)

    seen_timeout = {}

    def fake_run(args, **kwargs):
        seen_timeout["value"] = kwargs["timeout"]
        clock[0] += 0.9  # it2run itself consumes 90% of the budget
        return _completed()  # never writes result_path

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run, timeout=1.0)

    with pytest.raises(BackendError):
        backend.run({"operation": "verify", "pane_ref": "some-session-id"})

    # subprocess.run's own timeout is derived from the shared deadline,
    # computed before the call — with the clock still at 0.0, that is the
    # full budget.
    assert seen_timeout["value"] == pytest.approx(1.0)
    # The poll loop must not get a fresh full budget after subprocess.run
    # returns: total elapsed stays within self.timeout, not ~2x it.
    assert clock[0] <= 1.0 + 1e-9


def test_run_raises_backend_error_when_result_is_valid_json_but_not_an_object(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    """The case where the content is valid JSON but not a dict (the path
    where `result.get` raises AttributeError)."""

    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(request["result_path"])
        result_path.write_text("null", encoding="utf-8")
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    with pytest.raises(BackendError):
        backend.run({"operation": "verify", "pane_ref": "some-session-id"})


def test_run_polls_for_the_result_file_that_appears_after_it2run_returns(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    """`it2run` is fire-and-forget: it hands the script to iTerm2 and returns
    before the script has actually run. `subprocess.run` returning must not by
    itself be treated as "no result" — the result file can legitimately appear
    a little later, and `run()` must keep polling for it within its timeout
    budget instead of checking exactly once."""

    def fake_run(args, **kwargs):
        request_path = Path(args[2])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(request["result_path"])

        def write_result_later() -> None:
            # Atomic write (matches `iterm_script.write_result()`):
            # a plain `write_text()` here would let the polling loop observe
            # `result_path.exists()` mid-write and read an empty/partial file.
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=result_path.parent,
                prefix=".result-",
                delete=False,
            ) as temporary:
                json.dump({"ok": True, "exists": True}, temporary)
                temporary_path = Path(temporary.name)
            temporary_path.replace(result_path)

        threading.Timer(0.2, write_result_later).start()
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run, timeout=2)

    result = backend.run({"operation": "verify", "pane_ref": "some-session-id"})

    assert result == {"ok": True, "exists": True}


def test_run_converts_a_timeout_into_a_backend_error(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 20))

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run)

    with pytest.raises(BackendError, match="timed out"):
        backend.run({"operation": "verify", "pane_ref": "some-session-id"})


def test_run_raises_backend_error_immediately_when_it2run_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    """A non-zero `it2run` exit means the hand-off to iTerm2 itself failed —
    unlike its normal immediate exit 0, no result file will ever appear, so
    this must fail fast with the stderr detail instead of polling out the
    rest of a large `timeout`."""

    def fake_run(args, **kwargs):
        return _completed(returncode=1, stderr="boom")

    def fail_if_slept(seconds: float) -> None:
        raise AssertionError("must not poll after a non-zero it2run exit")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(time, "sleep", fail_if_slept)
    backend = Iterm2Backend(it2run=fake_it2run, timeout=20)

    with pytest.raises(BackendError, match="boom"):
        backend.run({"operation": "verify", "pane_ref": "some-session-id"})


def test_run_raises_backend_error_with_stderr_detail_when_polling_expires(
    monkeypatch: pytest.MonkeyPatch, fake_it2run: Path
) -> None:
    """The polling-expiration branch (result file never appears before the
    deadline) builds its detail message with the same stderr -> stdout ->
    fixed-fallback expression as the fail-fast branch above, but on its own
    `while` loop — this asserts that branch's detail directly instead of
    relying on the fail-fast branch's coverage as a proxy for it."""
    clock = [0.0]

    def fake_monotonic() -> float:
        return clock[0]

    def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(time, "sleep", fake_sleep)

    def fake_run(args, **kwargs):
        return _completed(returncode=0, stderr="boom")  # never writes result_path

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = Iterm2Backend(it2run=fake_it2run, timeout=1.0)

    with pytest.raises(BackendError, match="boom"):
        backend.run({"operation": "verify", "pane_ref": "some-session-id"})

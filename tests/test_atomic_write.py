import os
from pathlib import Path

import pytest

from sukuna.infrastructure.atomic_write import atomic_write


def test_atomic_write_creates_the_destination_with_str_content(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"

    atomic_write(path, "hello\n", prefix=".test-")

    assert path.read_text(encoding="utf-8") == "hello\n"


def test_atomic_write_creates_the_destination_with_bytes_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "out.bin"

    atomic_write(path, b"\x00\x01hello", prefix=".test-")

    assert path.read_bytes() == b"\x00\x01hello"


def test_atomic_write_replaces_existing_content(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"
    path.write_text("old", encoding="utf-8")

    atomic_write(path, "new", prefix=".test-")

    assert path.read_text(encoding="utf-8") == "new"


def test_atomic_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"

    atomic_write(path, "hello", prefix=".test-")

    assert list(tmp_path.iterdir()) == [path]


def test_atomic_write_fsyncs_the_temp_file_before_the_atomic_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fsync has no on-disk signature a full-path test could otherwise
    observe, so the only way to guard it against a silent future removal
    is to assert the call itself (same monkeypatch discipline as
    `registry.py`'s / `settings.py`'s / `claude_settings.py`'s equivalent
    tests, all of which delegate to this helper)."""
    real_fsync = os.fsync
    calls: list[int] = []

    def fake_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fake_fsync)

    atomic_write(tmp_path / "out.txt", "hello", prefix=".test-")

    assert len(calls) == 1

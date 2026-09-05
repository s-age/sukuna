"""Shared atomic-write discipline for on-disk files.

Used by `registry.py`, `settings.py`, and `claude_settings.py`. Covers
only the write mechanism itself, ignorant of any particular file format
(JSON, TOML, ...); locking around the read-modify-write critical section
stays the caller's responsibility.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: str | bytes, *, prefix: str) -> None:
    """Write `content` to `path` via a sibling temp file, `fsync` it, then
    atomically replace `path`. `content` may be `str` (written as UTF-8
    text) or `bytes` (written as-is). `prefix` names the temp file."""
    mode = "wb" if isinstance(content, bytes) else "w"
    encoding = None if isinstance(content, bytes) else "utf-8"
    with tempfile.NamedTemporaryFile(
        mode,
        encoding=encoding,
        dir=path.parent,
        prefix=prefix,
        delete=False,
    ) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)

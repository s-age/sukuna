#!/usr/bin/env python3
# pyright: reportPrivateImportUsage=false, reportAttributeAccessIssue=false
"""iTerm2 Python API entry point, launched only through `it2run`. Uses
only the standard library plus iTerm2's bundled `iterm2` module, so it
can run in iTerm2's own Python environment. The `# pyright:` line above
is scoped to this file only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol, cast

logger = logging.getLogger(__name__)


class _SizeLike(Protocol):
    """Structural stand-in for `iterm2.util.Size` (`.width`) -- matches both
    the real type and `tests/test_iterm_script.py`'s `_FakeSize`. Read-only
    (a `@property` here, not a plain attribute): these helpers only ever
    read `.width`, and a read-only Protocol member accepts both the real
    type's read-only property and the fakes' plain (read/write) attribute."""

    @property
    def width(self) -> int: ...


class _SessionLike(Protocol):
    """Structural stand-in for `iterm2.Session`, matching both the real type
    and `tests/test_iterm_script.py`'s `_FakeSession`. Duck-typing is
    intentional: these pure helpers must stay callable without `iterm2`
    installed. Read-only, same reasoning as `_SizeLike` -- the one
    place that writes `.preferred_size` (`resize()`) does so through the real
    `iterm2.Session` type, not through this Protocol."""

    @property
    def session_id(self) -> str: ...
    @property
    def preferred_size(self) -> _SizeLike: ...
    @property
    def grid_size(self) -> _SizeLike: ...


class _SplitterLike(Protocol):
    """Structural stand-in for `iterm2.Splitter`, matching both the real type
    and `tests/test_iterm_script.py`'s `_FakeSplitter`. Read-only, same
    reasoning as `_SizeLike`."""

    @property
    def children(self) -> Sequence[_LayoutNode]: ...
    @property
    def vertical(self) -> bool: ...
    @property
    def sessions(self) -> Sequence[_SessionLike]: ...


_LayoutNode = _SessionLike | _SplitterLike


class _TabLike(Protocol):
    """Structural stand-in for `iterm2.Tab`, matching both the real type and
    `tests/test_iterm_script.py`'s `_FakeTab`. Read-only, same reasoning as
    `_SizeLike`."""

    @property
    def root(self) -> _SplitterLike: ...


def write_result(**value: object) -> None:
    """Atomic write (same discipline as registry.py's `_write_unlocked`):
    writes to a temp file in the same directory, then swaps it in via
    `Path.replace()`."""
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=RESULT_PATH.parent,
        prefix=".result-",
        delete=False,
    ) as temporary:
        json.dump(value, temporary)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.replace(RESULT_PATH)


# --- pure, stdlib only (callable without `iterm2` imported) ---

_MIN_SAFE_PANE_WIDTH = 10  # Character-grid columns. Deliberate mirror of domain/service/pane_width.py's MIN_SAFE_PANE_WIDTH (this file runs in iTerm2's own Python environment and cannot import sukuna). tests/test_pane_width_parity.py detects drift between the two.


def compute_clamped_width(
    *,
    total_width: int,
    other_slot_widths: list[int],
    percent: int,
    min_safe_width: int = _MIN_SAFE_PANE_WIDTH,
) -> tuple[int, str | None]:
    """Deliberate mirror of domain/service/pane_width.py's
    compute_clamped_percent. When `other_slot_widths` is empty (no
    siblings), this function unconditionally returns `requested_width`
    as-is (no clamping)."""
    requested_width = round(total_width * percent / 100)
    if not other_slot_widths:
        return requested_width, None
    max_safe_width = total_width - len(other_slot_widths) * min_safe_width
    if max_safe_width < min_safe_width or requested_width > max_safe_width:
        safe_width = max(min_safe_width, max_safe_width)
        clamped_percent = max(1, min(percent, (safe_width * 100) // total_width))
        applied_width = round(total_width * clamped_percent / 100)
        if applied_width != requested_width:
            warning = (
                f"requested width {requested_width} of total {total_width} would squeeze "
                f"{len(other_slot_widths)} sibling slot(s) below {min_safe_width} columns; "
                f"clamped to {applied_width} ({clamped_percent}%)"
            )
            return applied_width, warning
    return requested_width, None


def _contains_pane_ref(node: _LayoutNode, pane_ref: str) -> bool:
    """True if `pane_ref` is `node` itself (a leaf Session) or one of the
    leaf Sessions within `node` (a Splitter-like column, via its flat
    `.sessions` list -- the same attribute `_representative_session()` and
    the per_session write in `resize()` use)."""
    if hasattr(node, "sessions"):
        splitter = cast(_SplitterLike, node)
        return any(session.session_id == pane_ref for session in splitter.sessions)
    session = cast(_SessionLike, node)
    return session.session_id == pane_ref


def _find_root_level_node(
    root: _SplitterLike, pane_ref: str
) -> tuple[_SplitterLike, _LayoutNode] | None:
    """Resolve `pane_ref` to the root splitter's direct child that contains
    it: a leaf Session if `pane_ref` names a plain side-by-side pane, or the
    nested Splitter (a stacked column) as a single unit if `pane_ref` names
    one of its members. Returns `(root, target_node)`. A stacked member
    resolves to its whole column, which `resize()` then writes to as one
    unit (per_session)."""
    for child in root.children:
        if _contains_pane_ref(child, pane_ref):
            return root, child
    return None


def _representative_session(node: _LayoutNode) -> _SessionLike:
    """A Splitter-like column's members share one width -- any one of them
    stands in for the column's width. A plain Session stands in for
    itself."""
    if hasattr(node, "sessions"):
        return cast(_SplitterLike, node).sessions[0]
    return cast(_SessionLike, node)


def _reject_unsupported_layout(
    splitter: _SplitterLike, other_children: Sequence[_LayoutNode]
) -> None:
    """Layout shape check (pure, callable without `iterm2` imported). Always
    permitted when there are no sibling slots. When siblings exist, only
    requires that they sit side-by-side (`splitter.vertical=True`); raises
    otherwise."""
    if not other_children:
        return
    if not splitter.vertical:
        raise RuntimeError(
            "resize target's immediate layout siblings are stacked (height axis), "
            "not side-by-side -- width resize is not defined at this level"
        )


def compute_equalize_target(heights: list[int]) -> int:
    """Column-equalization target height = sum of current heights / member
    count (floor division).

    Deliberate mirror of `domain.service.pane_height.equalize_heights` --
    this file runs in iTerm2's own Python plugin environment and cannot
    import the sukuna package (same constraint and solution as
    `_MIN_SAFE_PANE_WIDTH`). tests/test_equalize_parity.py detects drift
    between the two. The application loop ("apply to the first N-1
    members, the last absorbs the remainder") stays in `equalize()`."""
    return sum(heights) // len(heights)


# --- iterm2-dependent (reached only once `import iterm2` has run; see the __main__ block) ---


async def current_session(app: iterm2.App) -> iterm2.Session:
    window = app.current_window
    if (
        window is None
        or window.current_tab is None
        or window.current_tab.current_session is None
    ):
        raise RuntimeError("no active iTerm2 terminal session")
    return window.current_tab.current_session


def worker_profile(command: str) -> iterm2.LocalWriteOnlyProfile:
    """Create a session-local profile that starts the worker command."""
    profile = iterm2.LocalWriteOnlyProfile()  # type: ignore[no-untyped-call]
    profile.set_use_custom_command("Yes")
    profile.set_command(command)
    return profile


async def resolve_new_session(
    connection: iterm2.Connection, app: iterm2.App, session_id: str
) -> iterm2.Session:
    """`async_split_pane` can raise `SplitPaneException` even after iTerm2
    creates the session server-side: the local `App` model learns about it
    from an async notification that can lag the RPC response. Poll a
    refreshed app for the id before giving up."""
    session = app.get_session_by_id(session_id)
    for _ in range(10):
        if session is not None:
            return session
        await asyncio.sleep(0.2)
        app = cast(iterm2.App, await iterm2.async_get_app(connection))
        session = app.get_session_by_id(session_id)
    raise RuntimeError(
        f"iTerm2 created session {session_id} but it never became visible"
    )


async def spawn(connection: iterm2.Connection, app: iterm2.App) -> None:
    command = REQUEST["command"]
    anchor_id = REQUEST.get("anchor_pane_ref")
    profile = worker_profile(command)
    anchor = (
        app.get_session_by_id(anchor_id) if anchor_id else await current_session(app)
    )
    if anchor is None:
        raise RuntimeError("worker layout anchor does not exist")
    # tmux's "horizontal" (children arranged side by side) corresponds to
    # iTerm2's "vertical" (a vertical splitter line)
    vertical = REQUEST["split_direction"] == "horizontal"
    try:
        session = await anchor.async_split_pane(
            vertical=vertical,
            profile_customizations=profile,
        )
    except iterm2.session.SplitPaneException as error:
        match = re.search(r"No such session (\S+)", str(error))
        if match is None:
            raise
        session = await resolve_new_session(connection, app, match.group(1))
    # percent is not applied here: spawn always leaves a plain split in
    # place, and `usecase/spawn.py`'s `_spawn_one()` calls `resize()` (a
    # bounded retry with readback) as a separate step after spawn. A
    # follow-up write within the same process is a no-op, so it must run as
    # a separate process (a separate `it2run` invocation).
    # `REQUEST["active_pane_width"]` is not read in this function.
    window_id = session.window.window_id if session.window else None
    write_result(ok=True, pane_ref=session.session_id, window_ref=window_id)


async def close(connection: iterm2.Connection, app: iterm2.App) -> None:
    pane_ref = REQUEST["pane_ref"]
    session = app.get_session_by_id(pane_ref)
    if session is None:
        raise RuntimeError("iTerm2 worker session does not exist")
    await session.async_close(force=True)
    # `async_close()`'s RPC success does not guarantee the internal tree
    # (`tab.root`) has fully reflected the pane's disappearance -- the same
    # "an object reused within one connection can't be trusted" hazard as
    # `resolve_new_session()`. Symmetrically, re-fetch a fresh
    # `iterm2.async_get_app(connection)` each time to confirm removal. If
    # removal can't be confirmed, `async_close()` itself has not failed
    # (the RPC already succeeded), so this follows the same "succeed with
    # a warning" pattern as resize_warning/equalize_warning -- close
    # itself is never failed.
    for _ in range(10):
        fresh_app = cast(iterm2.App, await iterm2.async_get_app(connection))
        if fresh_app.get_session_by_id(pane_ref) is None:
            write_result(ok=True)
            return
        await asyncio.sleep(0.2)
    write_result(
        ok=True, close_warning="pane still visible after close() confirmation retries"
    )


async def verify(app: iterm2.App) -> None:
    session = app.get_session_by_id(REQUEST["pane_ref"])
    write_result(ok=True, exists=session is not None)


async def select_pane(app: iterm2.App) -> None:
    """`select_tab=True` also switches the containing tab (no separate
    window/tab-switch step needed, unlike tmux's `select-pane` which only
    affects the target's own window)."""
    session = app.get_session_by_id(REQUEST["pane_ref"])
    if session is None:
        raise RuntimeError("select_pane target session does not exist")
    await session.async_activate(select_tab=True, order_window_front=True)
    write_result(ok=True)


async def is_active(app: iterm2.App) -> None:
    """Fail-open when `current_session()` raises `RuntimeError`
    (unresolvable) -- falls to the safe side of "not active" (runs
    widen+activate)."""
    try:
        session = await current_session(app)
    except RuntimeError:
        write_result(ok=True, active=False)
        return
    write_result(ok=True, active=session.session_id == REQUEST["pane_ref"])


async def capture(app: iterm2.App) -> None:
    session = app.get_session_by_id(REQUEST["pane_ref"])
    if session is None:
        write_result(ok=True, exists=False, content=None)
        return
    contents = await session.async_get_screen_contents()
    lines = [contents.line(i).string for i in range(contents.number_of_lines)]
    write_result(ok=True, exists=True, content="\n".join(lines))


def _achieved_percent_from_splitter(
    found: tuple[_SplitterLike, _LayoutNode] | None,
) -> int | None:
    """Pure percent computation given a `_find_root_level_node()` result
    (or `None`). Uses `grid_size`, the real synced value, never
    `preferred_size` (that field goes stale after a write). `target`/
    `other` may each be a plain Session or a stacked Splitter column --
    read each through `_representative_session()`."""
    if found is None:
        return None
    splitter, target = found
    target_rep = _representative_session(target)
    other = [c for c in splitter.children if c is not target]
    if not other:
        return 100
    total = target_rep.grid_size.width + sum(
        _representative_session(c).grid_size.width for c in other
    )
    if total == 0:
        return None
    return round(target_rep.grid_size.width * 100 / total)


async def _readback_achieved_percent(
    connection: iterm2.Connection, pane_ref: str
) -> int | None:
    """The only readback that measures true: call
    `iterm2.async_get_app(connection)` again for a fresh snapshot and read
    `grid_size` from that -- reusing the already-held `app`/`session`
    returns a stale `grid_size`, the same staleness shape as
    `preferred_size` after a write."""
    fresh_app = cast(iterm2.App, await iterm2.async_get_app(connection))
    fresh_session = fresh_app.get_session_by_id(pane_ref)
    if fresh_session is None:
        return None
    fresh_tab = fresh_session.tab
    if fresh_tab is None:
        return None
    found = _find_root_level_node(fresh_tab.root, pane_ref)
    return _achieved_percent_from_splitter(found)


def _resolve_slot_widths(
    tab: _TabLike, pane_ref: str
) -> tuple[_LayoutNode, list[_LayoutNode], int, list[int]]:
    """Resolve `pane_ref` to its root-level layout node and read every slot's
    width, as prep for `compute_clamped_width()`.

    `target_node`/each of `other_children` may be a plain Session or a
    stacked Splitter column -- read each column's width through one
    representative member (a column's members share one width, both at
    rest and after a per_session write)."""
    found = _find_root_level_node(tab.root, pane_ref)
    if found is None:
        raise RuntimeError(
            "resize target session not found in its own tab's layout tree"
        )
    splitter, target_node = found
    other_children = [c for c in splitter.children if c is not target_node]
    _reject_unsupported_layout(splitter, other_children)
    target_session = _representative_session(target_node)
    other_slot_widths = [
        _representative_session(c).preferred_size.width for c in other_children
    ]
    total_width = target_session.preferred_size.width + sum(other_slot_widths)
    return target_node, other_children, total_width, other_slot_widths


def _write_column_width(target_node: _LayoutNode, applied_width: int) -> None:
    """Write `applied_width` to every member session of `target_node`'s
    column, preserving each member's own height (`per_session`) --
    writing a Splitter node's own `preferred_size` directly is
    impossible at the iTerm2 API level: `Splitter` has neither
    `preferred_size` nor `grid_size`. A plain-Session target is just a
    column of one."""
    members: Sequence[_SessionLike] = (
        cast(_SplitterLike, target_node).sessions
        if hasattr(target_node, "sessions")
        else [cast(_SessionLike, target_node)]
    )
    for member in members:
        # `member` is a real `iterm2.Session` at runtime (this function only
        # ever runs against the live iterm2 tree, never the test doubles) --
        # cast to reach the setter, which `_SessionLike` intentionally omits
        # (see its docstring).
        real_member = cast(iterm2.Session, member)
        real_member.preferred_size = iterm2.util.Size(
            applied_width, real_member.preferred_size.height
        )


def _resize_target(
    app: iterm2.App, pane_ref: str
) -> tuple[iterm2.Tab, _LayoutNode, int, str | None]:
    """Resolve `pane_ref`'s tab/layout node and compute its clamped
    applied width."""
    session = app.get_session_by_id(pane_ref)
    if session is None:
        raise RuntimeError("resize target session does not exist")
    tab = session.tab
    if tab is None:
        raise RuntimeError("resize target session has no containing tab")
    target_node, other_children, total_width, other_slot_widths = _resolve_slot_widths(
        tab, pane_ref
    )
    # When there are zero siblings (other_children is empty), there is no
    # one to share space with, so the only self-consistent value is 100%.
    # Treating percent as unconditionally 100 makes compute_clamped_width
    # always return total_width (the target pane's own current width)
    # unchanged, so total_width itself never drifts across repeated calls
    # and no compounding shrink can occur. This is not a special-cased
    # early return -- it's the existing general formula with one extra
    # condition.
    # Known limitation: this stops further shrinking, but does not
    # self-heal a pane that has already shrunk back to its original width
    # -- there is no independent source of truth for what that "original
    # full width" was.
    applied_width, warning = compute_clamped_width(
        total_width=total_width,
        other_slot_widths=other_slot_widths,
        percent=REQUEST["percent"] if other_children else 100,
    )
    return tab, target_node, applied_width, warning


async def resize(connection: iterm2.Connection, app: iterm2.App) -> None:
    pane_ref = REQUEST["pane_ref"]
    tab, target_node, applied_width, warning = _resize_target(app, pane_ref)
    _write_column_width(target_node, applied_width)
    await tab.async_update_layout()
    # Always re-fetch `iterm2.async_get_app(connection)` after
    # `async_update_layout()` before reading (see
    # `_readback_achieved_percent`) -- skipping this step just keeps
    # returning a stale value silently, with no error or warning.
    achieved_percent = await _readback_achieved_percent(connection, pane_ref)
    write_result(ok=True, resize_warning=warning, achieved_percent=achieved_percent)


async def equalize(app: iterm2.App) -> None:
    column = REQUEST["column_pane_refs"]
    sessions = [app.get_session_by_id(ref) for ref in column]
    if any(session is None for session in sessions):
        raise RuntimeError("equalize target session no longer exists")
    live_sessions = cast("list[iterm2.Session]", sessions)
    heights = [session.preferred_size.height for session in live_sessions]
    target = compute_equalize_target(heights)
    for session in live_sessions[:-1]:
        session.preferred_size = iterm2.util.Size(session.preferred_size.width, target)
    tab = live_sessions[0].tab
    if tab is None:
        raise RuntimeError("equalize target session has no containing tab")
    await tab.async_update_layout()
    write_result(ok=True)


async def resolve_window_ref(app: iterm2.App) -> None:
    """Counterpart to `spawn()`'s `session.window.window_id if session.window
    else None` -- resolves a still-alive pane's containing window on demand,
    for callers (self-resize) with no registered `WorkerRecord.window_ref`."""
    session = app.get_session_by_id(REQUEST["pane_ref"])
    if session is None:
        raise RuntimeError("pane no longer exists")
    window = session.window
    write_result(ok=True, window_ref=window.window_id if window else None)


async def get_window_frame(app: iterm2.App) -> None:
    """Keyed by `window_ref` (`App.get_window_by_id()`), not a pane's own
    session id."""
    window = app.get_window_by_id(REQUEST["window_ref"])
    if window is None:
        raise RuntimeError("window no longer exists")
    frame = await window.async_get_frame()
    write_result(
        ok=True,
        x=frame.origin.x,
        y=frame.origin.y,
        width=frame.size.width,
        height=frame.size.height,
    )


async def set_window_frame(app: iterm2.App) -> None:
    window = app.get_window_by_id(REQUEST["window_ref"])
    if window is None:
        raise RuntimeError("window no longer exists")
    frame = iterm2.util.Frame(
        iterm2.util.Point(REQUEST["x"], REQUEST["y"]),
        iterm2.util.Size(REQUEST["width"], REQUEST["height"]),
    )
    await window.async_set_frame(frame)
    write_result(ok=True)


_OPERATIONS: dict[str, Callable[[iterm2.Connection, iterm2.App], Awaitable[None]]] = {
    "spawn": spawn,
    "close": close,
    "verify": lambda _connection, app: verify(app),
    "capture": lambda _connection, app: capture(app),
    "select_pane": lambda _connection, app: select_pane(app),
    "is_active": lambda _connection, app: is_active(app),
    "equalize": lambda _connection, app: equalize(app),
    "resize": resize,
    "resolve_window_ref": lambda _connection, app: resolve_window_ref(app),
    "get_window_frame": lambda _connection, app: get_window_frame(app),
    "set_window_frame": lambda _connection, app: set_window_frame(app),
}


async def main(connection: iterm2.Connection) -> None:
    try:
        app = cast(iterm2.App, await iterm2.async_get_app(connection))
        operation = REQUEST["operation"]
        handler = _OPERATIONS.get(operation)
        if handler is None:
            raise RuntimeError(f"unknown operation: {operation}")
        await handler(connection, app)
    except Exception as error:
        # The actual stack trace only appears in iTerm2's Script Console --
        # logger.exception leaves it there while the CLI side reads
        # write_result's JSON.
        logger.exception("iTerm2 script operation failed")
        write_result(ok=False, error=str(error))


if __name__ == "__main__":
    import iterm2

    REQUEST = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    RESULT_PATH = Path(REQUEST["result_path"])
    iterm2.run_until_complete(main)

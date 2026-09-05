"""Unit tests for the pure functions in `iterm_script.py`, plus the handful
of `async def foo(app: iterm2.App)` functions whose bodies are themselves
pure attribute access (no `iterm2.*` module calls) -- see the "is_active"
section below for that second group.

`iterm_script.py` defers `import iterm2` and the `REQUEST`/`RESULT_PATH`
module-level bindings to `if __name__ == "__main__":`, so this module can be
imported by pytest without a runtime `iterm2` import. `resize()` itself (the
`app.get_session_by_id`/`await tab.async_update_layout()` part) stays
structurally untestable, same as the existing `spawn()`/`equalize()` --
those, the pure helpers they delegate to, `write_result()`'s file I/O, and
`is_active()`/`current_session()` are covered here.

`resize()`'s two helpers split along that same line:
`_resolve_slot_widths()` only walks the layout tree and reads `.preferred_
size.width` off Protocol-typed nodes, so it is covered below just like
`_find_root_level_node()`. `_write_column_width()` calls `iterm2.util.Size(
...)` and `cast(iterm2.Session, ...)` at runtime (the same `iterm2.*` calls
that make `resize()` itself untestable here), so it stays untested for the
same reason.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from sukuna.infrastructure.terminal import iterm_script
from sukuna.infrastructure.terminal.iterm_script import (
    _MIN_SAFE_PANE_WIDTH,
    _achieved_percent_from_splitter,
    _find_root_level_node,
    _reject_unsupported_layout,
    _resolve_slot_widths,
    compute_clamped_width,
    is_active,
    write_result,
)

if TYPE_CHECKING:
    import iterm2


class _FakeSession:
    """Duck-typed stand-in for `iterm2.Session`: a leaf node with a
    `session_id` and a `preferred_size`. Deliberately does NOT have a
    `.sessions` attribute, mirroring the real type (the production code
    tells sessions and splitters apart with `hasattr(child, "sessions")`)."""

    def __init__(
        self, session_id: str, width: int = 0, grid_width: int | None = None
    ) -> None:
        self.session_id = session_id
        self.preferred_size = _FakeSize(width)
        # `_achieved_percent_from_splitter()` reads `grid_size`, not
        # `preferred_size` (Q1/Q2: the latter is exactly what goes stale) --
        # defaults to `width` so existing `_find_root_level_node` tests that
        # never set it stay unaffected.
        self.grid_size = _FakeSize(grid_width if grid_width is not None else width)


class _FakeSize:
    def __init__(self, width: int) -> None:
        self.width = width


class _FakeSplitter:
    """Duck-typed stand-in for `iterm2.Splitter`: has `.children`, `.vertical`,
    and `.sessions` -- the last both makes `hasattr(x, "sessions")` true and,
    for a nested (stacked) column fixture, must be populated with the flat
    list of leaf Sessions it contains: `_contains_pane_ref()` and
    `_representative_session()` both read real values out of it."""

    def __init__(
        self,
        children: list["_FakeSession | _FakeSplitter"],
        vertical: bool,
        sessions: list["_FakeSession"] | None = None,
    ) -> None:
        self.children = children
        self.vertical = vertical
        self.sessions: list[_FakeSession] = sessions if sessions is not None else []


class _FakeTab:
    """Duck-typed stand-in for `iterm2.Tab`: `_resolve_slot_widths()` reads
    only `.root` off it."""

    def __init__(self, root: "_FakeSplitter") -> None:
        self.root = root


class _FakeIsActiveSession:
    """Duck-typed stand-in for `iterm2.Session`, `is_active()`/
    `current_session()`'s own view of it: only `.session_id` is read."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _FakeIsActiveTab:
    def __init__(self, current_session: "_FakeIsActiveSession | None") -> None:
        self.current_session = current_session


class _FakeIsActiveWindow:
    def __init__(self, current_tab: "_FakeIsActiveTab | None") -> None:
        self.current_tab = current_tab


class _FakeIsActiveApp:
    """Duck-typed stand-in for `iterm2.App`: `current_session()` walks
    `.current_window.current_tab.current_session`, raising `RuntimeError`
    if any hop is `None`."""

    def __init__(self, current_window: "_FakeIsActiveWindow | None") -> None:
        self.current_window = current_window


class TestComputeClampedWidth:
    def test_no_siblings_returns_requested_width_uncapped(self) -> None:
        applied_width, warning = compute_clamped_width(
            total_width=212, other_slot_widths=[], percent=50
        )

        assert applied_width == 106
        assert warning is None

    def test_owner_ruling_lone_pane_percent_100_is_a_no_op(self) -> None:
        """Single-pane callers pass percent=100 so that repeated resize()
        calls converge on total_width unchanged -- this is the direct test
        for the anti-compounding-shrink guarantee."""
        applied_width, warning = compute_clamped_width(
            total_width=212, other_slot_widths=[], percent=100
        )

        assert applied_width == 212
        assert warning is None

    def test_within_safe_bounds_is_not_clamped(self) -> None:
        applied_width, warning = compute_clamped_width(
            total_width=200, other_slot_widths=[80], percent=50
        )

        assert applied_width == 100
        assert warning is None

    def test_squeezing_a_sibling_below_the_floor_is_clamped_with_a_warning(
        self,
    ) -> None:
        applied_width, warning = compute_clamped_width(
            total_width=100, other_slot_widths=[10], percent=95
        )

        assert applied_width < 95
        assert warning is not None
        assert "clamped" in warning

    def test_multiple_siblings_each_reserve_the_floor(self) -> None:
        applied_width, warning = compute_clamped_width(
            total_width=100, other_slot_widths=[10, 10, 10], percent=90
        )

        assert applied_width <= 100 - 3 * _MIN_SAFE_PANE_WIDTH
        assert warning is not None


class TestFindRootLevelNode:
    """Root-level resolution: a stacked column's member resolves to the
    whole column (a Splitter node), not to the stacked splitter itself."""

    def test_finds_target_among_flat_siblings(self) -> None:
        target = _FakeSession("target")
        sibling = _FakeSession("sibling")
        root = _FakeSplitter([sibling, target], vertical=True)

        found = _find_root_level_node(root, "target")

        assert found == (root, target)

    def test_a_stacked_columns_member_resolves_to_the_whole_column(self) -> None:
        """A|[B,C] shape (card's spawn_placement.choose_anchor() shape): B
        is a member of the stacked column `nested` -- resolving B must
        return the column itself (`nested`), not descend into it, since
        `resize()` now treats the whole column as the write unit."""
        member_b = _FakeSession("b")
        member_c = _FakeSession("c")
        nested = _FakeSplitter(
            [member_b, member_c], vertical=False, sessions=[member_b, member_c]
        )
        side = _FakeSession("a")
        root = _FakeSplitter([side, nested], vertical=True)

        found = _find_root_level_node(root, "b")

        assert found == (root, nested)

    def test_a_three_member_stacked_columns_member_resolves_to_the_whole_column(
        self,
    ) -> None:
        """A|[B,C,D] shape."""
        member_b = _FakeSession("b")
        member_c = _FakeSession("c")
        member_d = _FakeSession("d")
        nested = _FakeSplitter(
            [member_b, member_c, member_d],
            vertical=False,
            sessions=[member_b, member_c, member_d],
        )
        side = _FakeSession("a")
        root = _FakeSplitter([side, nested], vertical=True)

        found = _find_root_level_node(root, "d")

        assert found == (root, nested)

    def test_returns_none_when_pane_ref_is_not_in_the_tree(self) -> None:
        root = _FakeSplitter([_FakeSession("a"), _FakeSession("b")], vertical=True)

        assert _find_root_level_node(root, "missing") is None

    def test_returns_none_when_pane_ref_is_not_in_a_stacked_column_either(self) -> None:
        nested = _FakeSplitter(
            [_FakeSession("b"), _FakeSession("c")],
            vertical=False,
            sessions=[_FakeSession("b"), _FakeSession("c")],
        )
        root = _FakeSplitter([_FakeSession("a"), nested], vertical=True)

        assert _find_root_level_node(root, "missing") is None

    def test_single_child_root_still_resolves(self) -> None:
        """0-sibling shape: consistency with the existing single-pane
        special case (`_reject_unsupported_layout`'s no-siblings branch,
        `resize()`'s `effective_percent = ... if other_children else 100`)."""
        target = _FakeSession("target")
        root = _FakeSplitter([target], vertical=False)

        found = _find_root_level_node(root, "target")

        assert found == (root, target)


class TestAchievedPercentFromSplitter:
    """Only the arithmetic is testable -- the fresh-`async_get_app` refetch
    it depends on is not (see `_readback_achieved_percent()`'s docstring)."""

    def test_none_found_returns_none(self) -> None:
        assert _achieved_percent_from_splitter(None) is None

    def test_no_siblings_is_100_percent(self) -> None:
        target = _FakeSession("target", grid_width=42)
        root = _FakeSplitter([target], vertical=False)

        assert _achieved_percent_from_splitter((root, target)) == 100

    def test_computes_ratio_from_grid_size_not_preferred_size(self) -> None:
        # preferred_size deliberately disagrees with grid_size here -- the
        # whole point (Q1/Q2) is that preferred_size is the stale one.
        target = _FakeSession("target", width=999, grid_width=140)
        sibling = _FakeSession("sibling", width=1, grid_width=60)
        root = _FakeSplitter([sibling, target], vertical=True)

        assert _achieved_percent_from_splitter((root, target)) == 70

    def test_zero_total_width_returns_none(self) -> None:
        target = _FakeSession("target", grid_width=0)
        sibling = _FakeSession("sibling", grid_width=0)
        root = _FakeSplitter([sibling, target], vertical=True)

        assert _achieved_percent_from_splitter((root, target)) is None

    def test_reads_a_stacked_sibling_columns_width_through_its_representative(
        self,
    ) -> None:
        """When the *other* child is a stacked column, its width comes from
        one representative member's `grid_size`, not from the (nonexistent)
        `grid_size` of the Splitter itself."""
        target = _FakeSession("target", grid_width=140)
        column_member_1 = _FakeSession("b", grid_width=60)
        column_member_2 = _FakeSession("c", grid_width=60)
        column = _FakeSplitter(
            [column_member_1, column_member_2],
            vertical=False,
            sessions=[column_member_1, column_member_2],
        )
        root = _FakeSplitter([column, target], vertical=True)

        assert _achieved_percent_from_splitter((root, target)) == 70

    def test_reads_the_targets_own_width_through_its_representative_when_target_is_a_column(
        self,
    ) -> None:
        column_member_1 = _FakeSession("b", grid_width=140)
        column_member_2 = _FakeSession("c", grid_width=140)
        column = _FakeSplitter(
            [column_member_1, column_member_2],
            vertical=False,
            sessions=[column_member_1, column_member_2],
        )
        sibling = _FakeSession("a", grid_width=60)
        root = _FakeSplitter([sibling, column], vertical=True)

        assert _achieved_percent_from_splitter((root, column)) == 70


class TestRejectUnsupportedLayout:
    def test_no_siblings_is_always_allowed_regardless_of_vertical(self) -> None:
        """A lone-pane tab's root Splitter measures `.vertical=False` --
        must not be rejected on that basis when there are no siblings to
        protect."""
        splitter = _FakeSplitter([], vertical=False)

        _reject_unsupported_layout(splitter, [])

    def test_siblings_side_by_side_all_plain_sessions_is_allowed(self) -> None:
        splitter = _FakeSplitter([], vertical=True)
        other_children = [_FakeSession("sibling-1"), _FakeSession("sibling-2")]

        _reject_unsupported_layout(splitter, other_children)

    def test_siblings_stacked_vertical_false_is_rejected(self) -> None:
        splitter = _FakeSplitter([], vertical=False)
        other_children = [_FakeSession("sibling")]

        try:
            _reject_unsupported_layout(splitter, other_children)
        except RuntimeError as error:
            assert "stacked" in str(error)
        else:
            raise AssertionError("expected RuntimeError for stacked siblings")

    def test_nested_splitter_sibling_is_allowed_when_root_is_vertical(self) -> None:
        """A stacked column's members share one width even after a write,
        and `resize()` reads/writes a Splitter sibling through
        `_representative_session()`/per_session -- so this shape is
        supported."""
        splitter = _FakeSplitter([], vertical=True)
        stacked_member_a = _FakeSession("stacked-a")
        stacked_member_b = _FakeSession("stacked-b")
        nested_sibling = _FakeSplitter(
            [stacked_member_a, stacked_member_b],
            vertical=False,
            sessions=[stacked_member_a, stacked_member_b],
        )

        _reject_unsupported_layout(splitter, [nested_sibling])

    def test_mixed_plain_and_nested_splitter_siblings_is_allowed_when_root_is_vertical(
        self,
    ) -> None:
        splitter = _FakeSplitter([], vertical=True)
        plain_sibling = _FakeSession("sibling")
        stacked_member_a = _FakeSession("stacked-a")
        stacked_member_b = _FakeSession("stacked-b")
        nested_sibling = _FakeSplitter(
            [stacked_member_a, stacked_member_b],
            vertical=False,
            sessions=[stacked_member_a, stacked_member_b],
        )

        _reject_unsupported_layout(splitter, [plain_sibling, nested_sibling])


class TestResolveSlotWidths:
    def test_resolves_target_and_reads_sibling_widths(self) -> None:
        target = _FakeSession("target", width=140)
        sibling = _FakeSession("sibling", width=60)
        tab = _FakeTab(_FakeSplitter([sibling, target], vertical=True))

        target_node, other_children, total_width, other_slot_widths = (
            _resolve_slot_widths(tab, "target")
        )

        assert target_node is target
        assert other_children == [sibling]
        assert other_slot_widths == [60]
        assert total_width == 200

    def test_no_siblings_returns_empty_widths(self) -> None:
        target = _FakeSession("target", width=212)
        tab = _FakeTab(_FakeSplitter([target], vertical=False))

        target_node, other_children, total_width, other_slot_widths = (
            _resolve_slot_widths(tab, "target")
        )

        assert target_node is target
        assert other_children == []
        assert other_slot_widths == []
        assert total_width == 212

    def test_reads_a_stacked_sibling_columns_width_through_its_representative(
        self,
    ) -> None:
        """Same reasoning as `_achieved_percent_from_splitter()`'s
        equivalent test above."""
        target = _FakeSession("target", width=140)
        column_member_1 = _FakeSession("b", width=60)
        column_member_2 = _FakeSession("c", width=60)
        column = _FakeSplitter(
            [column_member_1, column_member_2],
            vertical=False,
            sessions=[column_member_1, column_member_2],
        )
        tab = _FakeTab(_FakeSplitter([column, target], vertical=True))

        _, _, total_width, other_slot_widths = _resolve_slot_widths(tab, "target")

        assert other_slot_widths == [60]
        assert total_width == 200

    def test_raises_when_pane_ref_is_not_in_the_tree(self) -> None:
        tab = _FakeTab(
            _FakeSplitter([_FakeSession("a"), _FakeSession("b")], vertical=True)
        )

        try:
            _resolve_slot_widths(tab, "missing")
        except RuntimeError as error:
            assert "not found" in str(error)
        else:
            raise AssertionError("expected RuntimeError for unresolvable pane_ref")

    def test_raises_when_siblings_are_stacked_not_side_by_side(self) -> None:
        """Delegates layout-shape validation to `_reject_unsupported_layout()`
        -- same regression case as `TestRejectUnsupportedLayout` above."""
        target = _FakeSession("target", width=140)
        sibling = _FakeSession("sibling", width=60)
        tab = _FakeTab(_FakeSplitter([target, sibling], vertical=False))

        try:
            _resolve_slot_widths(tab, "target")
        except RuntimeError as error:
            assert "stacked" in str(error)
        else:
            raise AssertionError("expected RuntimeError for stacked siblings")


def test_write_result_fsyncs_the_temp_file_before_the_atomic_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same discipline as `Registry._write_unlocked` -- fsync has no
    on-disk signature a full-path test could otherwise observe, so the
    only way to guard it against a silent future removal is to assert the
    call itself."""
    real_fsync = os.fsync
    calls: list[int] = []

    def fake_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fake_fsync)
    result_path = tmp_path / "result.json"
    monkeypatch.setattr(iterm_script, "RESULT_PATH", result_path, raising=False)

    write_result(ok=True)

    assert len(calls) == 1
    assert json.loads(result_path.read_text(encoding="utf-8")) == {"ok": True}


# -- is_active --
#
# `is_active()` and the `current_session()` it delegates to touch only
# plain attribute access, no `iterm2.*` module calls (unlike `spawn()`/
# `resize()`/`equalize()`, which stay "structurally untestable" per this
# file's module docstring) -- so, like `write_result()`, they are testable
# here through duck-typed fakes and `REQUEST`/`RESULT_PATH` monkeypatches.


def _set_request_and_result_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, pane_ref: str
) -> Path:
    result_path = tmp_path / "result.json"
    monkeypatch.setattr(iterm_script, "RESULT_PATH", result_path, raising=False)
    monkeypatch.setattr(iterm_script, "REQUEST", {"pane_ref": pane_ref}, raising=False)
    return result_path


def test_is_active_reports_true_when_the_current_session_matches_pane_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_path = _set_request_and_result_path(
        monkeypatch, tmp_path, pane_ref="target-id"
    )
    app = _FakeIsActiveApp(
        _FakeIsActiveWindow(_FakeIsActiveTab(_FakeIsActiveSession("target-id")))
    )

    asyncio.run(is_active(cast("iterm2.App", app)))

    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "ok": True,
        "active": True,
    }


def test_is_active_reports_false_when_the_current_session_is_a_different_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_path = _set_request_and_result_path(
        monkeypatch, tmp_path, pane_ref="target-id"
    )
    app = _FakeIsActiveApp(
        _FakeIsActiveWindow(_FakeIsActiveTab(_FakeIsActiveSession("other-id")))
    )

    asyncio.run(is_active(cast("iterm2.App", app)))

    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "ok": True,
        "active": False,
    }


def test_is_active_fails_open_when_current_session_cannot_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`current_session()`'s `RuntimeError` (no active iTerm2 window/tab/
    session) must not propagate -- fail-open to "not active" so the
    caller still runs widen+activate."""
    result_path = _set_request_and_result_path(
        monkeypatch, tmp_path, pane_ref="target-id"
    )
    app = _FakeIsActiveApp(current_window=None)

    asyncio.run(is_active(cast("iterm2.App", app)))

    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "ok": True,
        "active": False,
    }

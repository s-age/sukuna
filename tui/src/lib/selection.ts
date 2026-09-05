import { selectableIndices } from "./flattenTree.js";
import type { Row } from "./flattenTree.js";

export function initialSelection(rows: Row[]): number | null {
  const indices = selectableIndices(rows);

  return indices.length > 0 ? indices[0] : null;
}

export function moveSelection(
  rows: Row[],
  current: number | null,
  delta: number,
): number | null {
  const indices = selectableIndices(rows);
  if (indices.length === 0) return null;

  if (current === null) {
    return delta >= 0 ? indices[0] : indices[indices.length - 1];
  }

  const currentPosition = indices.indexOf(current);
  const basePosition = currentPosition === -1 ? 0 : currentPosition;
  const nextPosition = Math.min(
    Math.max(basePosition + delta, 0),
    indices.length - 1,
  );

  return indices[nextPosition];
}

export function scrollWindow(
  totalRows: number,
  selectedRowIndex: number | null,
  viewportHeight: number,
): { start: number; end: number } {
  const visible = Math.max(1, viewportHeight);
  if (totalRows <= visible) return { start: 0, end: totalRows };

  const anchor = selectedRowIndex ?? 0;
  const halfVisible = Math.floor(visible / 2);
  let start = anchor - halfVisible;
  if (start < 0) start = 0;
  if (start + visible > totalRows) start = Math.max(0, totalRows - visible);

  return { start, end: start + visible };
}

// "relative offset from the tail" -- `currentOffset` counts how many
// lines back from the newest entry the viewport's bottom edge sits.
// up/k (delta=-1) scrolls back (offset grows), down/j (delta=+1)
// scrolls toward the tail (offset shrinks). Clamped to
// [0, totalEntries - viewportHeight] so it never scrolls past either
// end.
export function moveHistoryOffset(
  currentOffset: number,
  delta: number,
  totalEntries: number,
  viewportHeight: number,
): number {
  const maxOffset = Math.max(0, totalEntries - viewportHeight);
  const next = currentOffset - delta;

  return Math.min(Math.max(next, 0), maxOffset);
}

// The windowing math `SessionHistory` renders from -- factored out
// alongside `scrollWindow` so it's unit-testable without a React
// renderer (this package has no component-testing harness). Clamps
// `scrollOffset` itself (not just the caller's state)
// so a shrinking `entries`/`height` -- e.g. a terminal resize, or
// switching to a worker with a shorter log -- can never yield a
// negative or out-of-range window even if the offset state hasn't been
// re-clamped yet.
export function historyWindow(
  totalEntries: number,
  height: number,
  scrollOffset: number,
): { start: number; end: number } {
  const maxOffset = Math.max(0, totalEntries - height);
  const clampedOffset = Math.min(Math.max(scrollOffset, 0), maxOffset);
  const end = totalEntries - clampedOffset;
  const start = Math.max(0, end - height);

  return { start, end };
}

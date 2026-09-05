import { describe, it, expect } from "vitest";
import {
  historyWindow,
  initialSelection,
  moveHistoryOffset,
  moveSelection,
  scrollWindow,
} from "../selection.js";
import type { Row } from "../flattenTree.js";

function makeRows(): Row[] {
  const node = (name: string) => ({
    name,
    goal: null,
    state: "ready",
    updated_at: "2026-08-28T00:00:00+00:00",
    children: [],
    session_log_path: null,
  });

  return [
    { kind: "group", key: "session-a", depth: 0 },
    { kind: "node", node: node("worker-1"), depth: 1 },
    { kind: "marker", text: "(cycle)", depth: 1 },
    { kind: "node", node: node("worker-2"), depth: 1 },
    { kind: "node", node: node("worker-3"), depth: 1 },
  ];
}

describe("initialSelection", () => {
  it("selects the first row, which is the group row", () => {
    expect(initialSelection(makeRows())).toBe(0);
  });

  it("returns null when there are no node or group rows", () => {
    expect(initialSelection([{ kind: "marker", text: "(cycle)", depth: 0 }])).toBeNull();
  });
});

describe("moveSelection", () => {
  it("moves forward from the group row to the first node row", () => {
    expect(moveSelection(makeRows(), 0, 1)).toBe(1);
  });

  it("moves forward to the next node row, skipping markers", () => {
    expect(moveSelection(makeRows(), 1, 1)).toBe(3);
  });

  it("moves backward from a node row to the group row", () => {
    expect(moveSelection(makeRows(), 1, -1)).toBe(0);
  });

  it("moves backward to the previous node row", () => {
    expect(moveSelection(makeRows(), 3, -1)).toBe(1);
  });

  it("clamps at the last node row", () => {
    expect(moveSelection(makeRows(), 4, 1)).toBe(4);
  });

  it("clamps at the group row", () => {
    expect(moveSelection(makeRows(), 0, -1)).toBe(0);
  });

  it("selects the group row when nothing is selected and moving forward", () => {
    expect(moveSelection(makeRows(), null, 1)).toBe(0);
  });

  it("selects the last node row when nothing is selected and moving backward", () => {
    expect(moveSelection(makeRows(), null, -1)).toBe(4);
  });

  it("returns null when there are no node or group rows at all", () => {
    expect(moveSelection([{ kind: "marker", text: "(cycle)", depth: 0 }], null, 1)).toBeNull();
  });
});

describe("scrollWindow", () => {
  it("shows the whole list when it fits in the viewport", () => {
    expect(scrollWindow(5, 2, 10)).toEqual({ start: 0, end: 5 });
  });

  it("centers the selection within the viewport when the list overflows", () => {
    expect(scrollWindow(20, 10, 4)).toEqual({ start: 8, end: 12 });
  });

  it("clamps the window to the start of the list", () => {
    expect(scrollWindow(20, 0, 4)).toEqual({ start: 0, end: 4 });
  });

  it("clamps the window to the end of the list", () => {
    expect(scrollWindow(20, 19, 4)).toEqual({ start: 16, end: 20 });
  });

  it("anchors to the top when nothing is selected", () => {
    expect(scrollWindow(20, null, 4)).toEqual({ start: 0, end: 4 });
  });
});

describe("moveHistoryOffset", () => {
  it("scrolling up (delta=-1) increases the offset from the tail", () => {
    expect(moveHistoryOffset(0, -1, 100, 10)).toBe(1);
  });

  it("scrolling down (delta=1) decreases the offset toward the tail", () => {
    expect(moveHistoryOffset(5, 1, 100, 10)).toBe(4);
  });

  it("clamps at 0 -- cannot scroll past the tail", () => {
    expect(moveHistoryOffset(0, 1, 100, 10)).toBe(0);
  });

  it("clamps at totalEntries - viewportHeight -- cannot scroll past the top", () => {
    expect(moveHistoryOffset(90, -1, 100, 10)).toBe(90);
  });

  it("clamps to 0 when the log is shorter than the viewport", () => {
    expect(moveHistoryOffset(0, -1, 3, 10)).toBe(0);
  });

  it("repeated up presses clamp exactly at the top, never overshooting", () => {
    let offset = 0;
    for (let i = 0; i < 50; i += 1) offset = moveHistoryOffset(offset, -1, 20, 10);
    expect(offset).toBe(10);
  });
});

describe("historyWindow", () => {
  it("shows the tail when scrollOffset is 0", () => {
    expect(historyWindow(20, 5, 0)).toEqual({ start: 15, end: 20 });
  });

  it("shows the whole list when it fits in the viewport", () => {
    expect(historyWindow(3, 5, 0)).toEqual({ start: 0, end: 3 });
  });

  it("shifts the window back by scrollOffset lines from the tail", () => {
    expect(historyWindow(20, 5, 4)).toEqual({ start: 11, end: 16 });
  });

  it("clamps a scrollOffset beyond the top of the log", () => {
    expect(historyWindow(20, 5, 999)).toEqual({ start: 0, end: 5 });
  });

  it("clamps a negative scrollOffset back to the tail", () => {
    expect(historyWindow(20, 5, -3)).toEqual({ start: 15, end: 20 });
  });

  it("never yields a negative start when entries are shorter than height", () => {
    expect(historyWindow(2, 5, 0)).toEqual({ start: 0, end: 2 });
  });
});

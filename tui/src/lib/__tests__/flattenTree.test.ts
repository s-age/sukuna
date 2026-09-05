import { describe, it, expect } from "vitest";
import { flattenPayload, selectableIndices } from "../flattenTree.js";
import type { TreePayload } from "../../types.js";

function node(name: string, children: TreePayload["groups"][string] = []) {
  return {
    name,
    goal: null,
    state: "ready",
    updated_at: "2026-08-28T00:00:00+00:00",
    children,
    session_log_path: null,
  };
}

describe("flattenPayload", () => {
  it("emits a group row followed by its root nodes at depth 1", () => {
    const payload: TreePayload = {
      groups: { "session-a": [node("worker-1")] },
      hidden_parent_groups: 0,
      group_session_log_paths: {},
    };

    const rows = flattenPayload(payload);

    expect(rows).toEqual([
      { kind: "group", key: "session-a", depth: 0 },
      { kind: "node", node: node("worker-1"), depth: 1 },
    ]);
  });

  it("recurses into children with increasing depth", () => {
    const payload: TreePayload = {
      groups: { "session-a": [node("parent", [node("child")])] },
      hidden_parent_groups: 0,
      group_session_log_paths: {},
    };

    const rows = flattenPayload(payload);

    expect(rows.map((row) => row.depth)).toEqual([0, 1, 2]);
    expect(rows[2]).toEqual({ kind: "node", node: node("child"), depth: 2 });
  });

  it("renders a cycle-marker string as a marker row, not a node row", () => {
    const payload: TreePayload = {
      groups: {
        "session-a": [node("parent", ["(cycle detected: 'parent' already an ancestor)"])],
      },
      hidden_parent_groups: 0,
      group_session_log_paths: {},
    };

    const rows = flattenPayload(payload);

    expect(rows[2]).toEqual({
      kind: "marker",
      text: "(cycle detected: 'parent' already an ancestor)",
      depth: 2,
    });
  });

  it("preserves group iteration order across multiple groups", () => {
    const payload: TreePayload = {
      groups: {
        "(unknown parent)": [node("a")],
        "session-b": [node("b")],
      },
      hidden_parent_groups: 0,
      group_session_log_paths: {},
    };

    const rows = flattenPayload(payload);

    expect(rows.map((row) => (row.kind === "group" ? row.key : null))).toEqual([
      "(unknown parent)",
      null,
      "session-b",
      null,
    ]);
  });
});

describe("selectableIndices", () => {
  it("returns the indices of node and group rows, skipping markers", () => {
    const rows = [
      { kind: "group" as const, key: "g", depth: 0 },
      { kind: "node" as const, node: node("a"), depth: 1 },
      { kind: "marker" as const, text: "(cycle)", depth: 1 },
      { kind: "node" as const, node: node("b"), depth: 1 },
    ];

    expect(selectableIndices(rows)).toEqual([0, 1, 3]);
  });

  it("returns just the group row's index when there are no node rows", () => {
    const rows = [{ kind: "group" as const, key: "g", depth: 0 }];

    expect(selectableIndices(rows)).toEqual([0]);
  });

  it("returns an empty array when there are no node or group rows", () => {
    const rows = [{ kind: "marker" as const, text: "(cycle)", depth: 0 }];

    expect(selectableIndices(rows)).toEqual([]);
  });
});

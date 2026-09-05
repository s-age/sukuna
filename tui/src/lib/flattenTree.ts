import { isTreeNode } from "../types.js";
import type { TreeNode, TreePayload } from "../types.js";

export type Row =
  | { kind: "group"; key: string; depth: number }
  | { kind: "node"; node: TreeNode; depth: number }
  | { kind: "marker"; text: string; depth: number };

function flattenItem(item: TreeNode | string, depth: number, out: Row[]): void {
  if (!isTreeNode(item)) {
    out.push({ kind: "marker", text: item, depth });

    return;
  }

  out.push({ kind: "node", node: item, depth });
  for (const child of item.children) {
    flattenItem(child, depth + 1, out);
  }
}

export function flattenPayload(payload: TreePayload): Row[] {
  const rows: Row[] = [];

  for (const [key, items] of Object.entries(payload.groups)) {
    rows.push({ kind: "group", key, depth: 0 });
    for (const item of items) {
      flattenItem(item, 1, rows);
    }
  }

  return rows;
}

export function selectableIndices(rows: Row[]): number[] {
  const indices: number[] = [];

  rows.forEach((row, index) => {
    if (row.kind === "node" || row.kind === "group") indices.push(index);
  });

  return indices;
}

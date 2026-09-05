export type TreeNode = {
  name: string;
  goal: string | null;
  state: string;
  updated_at: string;
  children: Array<TreeNode | string>;
  session_log_path: string | null;
};

export type TreePayload = {
  groups: Record<string, Array<TreeNode | string>>;
  hidden_parent_groups: number;
  group_session_log_paths: Record<string, string | null>;
};

export function isTreeNode(item: TreeNode | string): item is TreeNode {
  return typeof item !== "string";
}

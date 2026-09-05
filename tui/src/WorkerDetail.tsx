import React from "react";
import { Box, Text } from "ink";
import type { Row } from "./lib/flattenTree.js";
import { formatLocalTimestamp } from "./lib/formatTimestamp.js";
import { useForegroundColor } from "./ForegroundColorContext.js";

type WorkerDetailProps = {
  selectedRow: Row | null;
};

export function WorkerDetail({ selectedRow }: WorkerDetailProps): React.JSX.Element {
  const foregroundColor = useForegroundColor();

  if (selectedRow === null) {
    return (
      <Text color={foregroundColor} dimColor>
        No worker selected.
      </Text>
    );
  }

  if (selectedRow.kind === "marker") {
    return (
      <Text color={foregroundColor} dimColor>
        {selectedRow.text}
      </Text>
    );
  }

  if (selectedRow.kind === "group") {
    return (
      <Text color={foregroundColor} dimColor>
        {selectedRow.key}
      </Text>
    );
  }

  const { node } = selectedRow;

  return (
    <Box flexDirection="column">
      {/* All four lines truncate at the end, matching SessionHistory's
          wrap style. Without `wrap="truncate"` here, Ink's default wrap
          would spread a long line across multiple rows and silently
          push e.g. `updated_at` past the pane's `overflow="hidden"`
          bottom edge. */}
      <Text color={foregroundColor} bold wrap="truncate">
        {node.name}
      </Text>
      <Text color={foregroundColor} wrap="truncate">
        state: {node.state}
      </Text>
      <Text color={foregroundColor} wrap="truncate">
        goal: {node.goal ?? "(none)"}
      </Text>
      <Text color={foregroundColor} wrap="truncate">
        updated_at: {formatLocalTimestamp(node.updated_at)}
      </Text>
    </Box>
  );
}

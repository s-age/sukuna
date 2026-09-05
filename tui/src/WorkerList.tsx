import React from "react";
import { Box, Text } from "ink";
import type { Row } from "./lib/flattenTree.js";
import { ACTIVE_COLOR } from "./lib/theme.js";
import { useForegroundColor } from "./ForegroundColorContext.js";

type WorkerListProps = {
  rows: Row[];
  selectedIndex: number | null;
  windowStart: number;
  windowEnd: number;
};

function rowLabel(row: Row): string {
  if (row.kind === "group") return row.key;
  if (row.kind === "marker") return row.text;

  return row.node.name;
}

export function WorkerList({
  rows,
  selectedIndex,
  windowStart,
  windowEnd,
}: WorkerListProps): React.JSX.Element {
  const foregroundColor = useForegroundColor();
  const visible = rows.slice(windowStart, windowEnd);

  return (
    <Box flexDirection="column">
      {visible.map((row, offset) => {
        const realIndex = windowStart + offset;
        const isSelected = realIndex === selectedIndex;
        const indent = "  ".repeat(row.depth);

        return (
          <Box key={`${realIndex}-${rowLabel(row)}`}>
            {/* The selected row keeps the fixed active color; every
                other row falls back to the detected foregroundColor. */}
            {/* Worker names routinely exceed the left pane's content
                width (measured: this session's own 42-char name against
                a ~25-27 col pane at 80 cols), which breaks
                scrollWindow's "1 row = 1 array element" assumption via
                Ink's default wrap. wrap="truncate" restores that
                invariant. */}
            <Text
              bold={row.kind === "group"}
              color={isSelected ? ACTIVE_COLOR : foregroundColor}
              wrap="truncate"
            >
              {isSelected ? "> " : "  "}
              {indent}
              {rowLabel(row)}
            </Text>
          </Box>
        );
      })}
    </Box>
  );
}

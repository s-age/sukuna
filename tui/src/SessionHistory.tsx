import React from "react";
import { Box, Text } from "ink";
import type { HistoryLine } from "./lib/historyLines.js";
import { historyWindow } from "./lib/selection.js";
import { useForegroundColor } from "./ForegroundColorContext.js";

type SessionHistoryProps = {
  sessionLogPath: string | null;
  lines: HistoryLine[];
  height: number;
  scrollOffset: number;
};

export function SessionHistory({
  sessionLogPath,
  lines,
  height,
  scrollOffset,
}: SessionHistoryProps): React.JSX.Element {
  // foregroundColor is applied here too, for consistency with every
  // other Text in the tree -- otherwise this would be the one pane
  // still defaulting to terminal color instead of the detected
  // black/white choice.
  const foregroundColor = useForegroundColor();

  if (sessionLogPath === null) {
    return (
      <Text color={foregroundColor} dimColor>
        No session log available.
      </Text>
    );
  }

  // Independently scrollable via `scrollOffset` (lines back from the
  // tail, owned by `App.tsx` and driven by j/k/arrows/PgUp/PgDn while
  // this pane is focused). `historyWindow` re-clamps on every render,
  // so a shrinking `lines`/`height` (terminal resize, a shorter log
  // after switching workers) never produces a negative or
  // out-of-range window.
  //
  // `lines` are already wrapped to the pane's content width by
  // `flattenEntriesToLines` upstream, so `historyWindow`'s
  // "totalEntries" unit is a wrapped display row here, not a
  // session-log entry -- the function itself is unit-agnostic.
  const { start, end } = historyWindow(lines.length, height, scrollOffset);
  const visible = lines.slice(start, end);

  if (visible.length === 0) {
    return (
      <Text color={foregroundColor} dimColor>
        (no history yet)
      </Text>
    );
  }

  return (
    <Box flexDirection="column">
      {visible.map((line, index) => (
        // wrap="truncate" here is a safety net, not the primary wrapping
        // mechanism -- `line.text` is already one wrapped display row.
        // It only fires on a measurement edge case (e.g. wide-glyph
        // width mismatch between wrap-ansi and the terminal), and is a
        // no-op otherwise.
        <Text key={index} color={foregroundColor} dimColor={line.role === "tool"} wrap="truncate">
          {line.text}
        </Text>
      ))}
    </Box>
  );
}

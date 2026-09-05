import React, { useEffect, useMemo, useState } from "react";
import { Box, useApp, useStdout } from "ink";
import type { TreePayload } from "./types.js";
import { flattenPayload } from "./lib/flattenTree.js";
import {
  initialSelection,
  moveHistoryOffset,
  moveSelection,
  scrollWindow,
} from "./lib/selection.js";
import { flattenEntriesToLines } from "./lib/historyLines.js";
import { WorkerList } from "./WorkerList.js";
import { WorkerDetail } from "./WorkerDetail.js";
import { SessionHistory } from "./SessionHistory.js";
import { StatusBar } from "./StatusBar.js";
import { useKeyHandler } from "./useKeyHandler.js";
import type { FocusTarget } from "./useKeyHandler.js";
import { useSessionLog } from "./useSessionLog.js";
import { ACTIVE_COLOR } from "./lib/theme.js";

// Single border, left + right: the history pane's content width for
// wrapping must match the width Ink actually renders inside its bordered
// Box, or the wrap-line count computed here diverges from what Ink draws.
const RIGHT_BORDER_WIDTH = 2;

// Default focus is the left pane.

// Fixed height, not a terminal-height-relative ratio -- the detail
// view's content is always exactly 4 lines.
const RIGHT_TOP_HEIGHT = 6;

type AppProps = {
  payload: TreePayload;
};

export function App({ payload }: AppProps): React.JSX.Element {
  const { exit } = useApp();
  const { stdout } = useStdout();
  const termRows = stdout.rows ?? 24;
  const termCols = stdout.columns ?? 80;

  const rows = useMemo(() => flattenPayload(payload), [payload]);
  const [selected, setSelected] = useState<number | null>(() => initialSelection(rows));
  const [focus, setFocus] = useState<FocusTarget>("left");
  const [historyOffset, setHistoryOffset] = useState(0);

  const usableCols = termCols - 1;
  const mainHeight = termRows - 2;
  const leftWidth = Math.floor(usableCols * 0.4);
  const rightWidth = usableCols - leftWidth;
  const viewportHeight = Math.max(1, mainHeight - 2);
  const { start, end } = scrollWindow(rows.length, selected, viewportHeight);
  const selectedRow = selected !== null ? (rows[selected] ?? null) : null;

  const rightTopHeight = Math.min(mainHeight, RIGHT_TOP_HEIGHT);
  const rightBottomHeight = Math.max(0, mainHeight - rightTopHeight);
  const historyViewportHeight = Math.max(1, rightBottomHeight - 2);

  const sessionLogPath =
    selectedRow?.kind === "node"
      ? (selectedRow.node.session_log_path ?? null)
      : selectedRow?.kind === "group"
        ? (payload.group_session_log_paths[selectedRow.key] ?? null)
        : null;
  const historyEntries = useSessionLog(sessionLogPath);
  const historyContentWidth = Math.max(1, rightWidth - RIGHT_BORDER_WIDTH);
  const wrappedHistoryLines = useMemo(
    () => flattenEntriesToLines(historyEntries, historyContentWidth),
    [historyEntries, historyContentWidth],
  );

  // Switching the selected worker/group resets the history scroll back
  // to the tail -- otherwise the pane would open mid-scroll into a log
  // that has nothing to do with the new selection. This effect owns only
  // `historyOffset`; it is fully independent of `useSessionLog`'s own
  // polling effect (keyed on `path` inside that hook) -- neither one's
  // dependency array or cleanup references the other, so there is no
  // interference or double-subscription path between them.
  useEffect(() => {
    setHistoryOffset(0);
  }, [sessionLogPath]);

  useKeyHandler({
    // The history pane scrolls independently via a tail-relative offset
    // (`historyOffset`, clamped by `moveHistoryOffset`). This state is a
    // plain derived number with no timer/subscription of its own.
    onMove: (delta) => {
      if (focus === "history") {
        setHistoryOffset((current) =>
          moveHistoryOffset(current, delta, wrappedHistoryLines.length, historyViewportHeight),
        );

        return;
      }
      if (focus !== "left") return;
      setSelected((current) => moveSelection(rows, current, delta));
    },
    // PgUp/PgDn move a full viewport at a time, reusing
    // moveSelection/moveHistoryOffset unchanged -- both already clamp an
    // arbitrary-size delta, so a viewport-height delta is just a bigger
    // version of the same j/k/arrow move.
    onPageMove: (direction) => {
      if (focus === "history") {
        setHistoryOffset((current) =>
          moveHistoryOffset(
            current,
            direction * historyViewportHeight,
            wrappedHistoryLines.length,
            historyViewportHeight,
          ),
        );

        return;
      }
      setSelected((current) => moveSelection(rows, current, direction * viewportHeight));
    },
    onQuit: () => exit(),
    onToggleFocus: () => setFocus((current) => (current === "left" ? "history" : "left")),
    onFocusTo: (target) => setFocus(target),
  });

  return (
    <Box flexDirection="column" height={termRows}>
      <Box height={mainHeight}>
        <Box
          width={leftWidth}
          flexDirection="column"
          borderStyle="single"
          borderColor={focus === "left" ? ACTIVE_COLOR : undefined}
          overflow="hidden"
        >
          <WorkerList rows={rows} selectedIndex={selected} windowStart={start} windowEnd={end} />
        </Box>
        <Box width={rightWidth} flexDirection="column">
          <Box
            height={rightTopHeight}
            flexDirection="column"
            borderStyle="single"
            overflow="hidden"
          >
            <WorkerDetail selectedRow={selectedRow} />
          </Box>
          <Box
            height={rightBottomHeight}
            flexDirection="column"
            borderStyle="single"
            borderColor={focus === "history" ? ACTIVE_COLOR : undefined}
            overflow="hidden"
          >
            <SessionHistory
              sessionLogPath={sessionLogPath}
              lines={wrappedHistoryLines}
              height={historyViewportHeight}
              scrollOffset={historyOffset}
            />
          </Box>
        </Box>
      </Box>
      <Box height={1}>
        <StatusBar hiddenParentGroups={payload.hidden_parent_groups} />
      </Box>
    </Box>
  );
}

import type { SessionLogEntry } from "./sessionLog.js";
import { wrapLine } from "./wrapText.js";

export type HistoryLine = { role: SessionLogEntry["role"]; text: string };

const ROLE_LABEL: Record<SessionLogEntry["role"], string> = {
  user: "user",
  assistant: "assistant",
  tool: "tool",
};

// Whitespace normalization (collapsing `\s+` to one space, trimming):
// wrapping a raw multi-line entry as-is would preserve its embedded
// newlines.
function formatEntry(entry: SessionLogEntry): string {
  const oneLine = entry.text.replace(/\s+/g, " ").trim();

  return `[${ROLE_LABEL[entry.role]}] ${oneLine}`;
}

export function flattenEntriesToLines(entries: SessionLogEntry[], width: number): HistoryLine[] {
  const lines: HistoryLine[] = [];
  for (const entry of entries) {
    for (const line of wrapLine(formatEntry(entry), width)) {
      lines.push({ role: entry.role, text: line });
    }
  }

  return lines;
}

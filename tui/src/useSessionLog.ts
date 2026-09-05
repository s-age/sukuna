import { readFileSync } from "node:fs";
import { useEffect, useState } from "react";
import { parseSessionLog } from "./lib/sessionLog.js";
import type { SessionLogEntry } from "./lib/sessionLog.js";

// Plain interval polling, full re-read per tick. The transcript is
// read whole rather than tailed -- simplicity over throughput, since
// `session_log_path` targets are a single worker's log at a time, not
// every worker at once. A line still being written when a read lands
// is handled by `parseSessionLog()` silently dropping whatever fails
// `JSON.parse` (deferred to that defensive skip rather than any
// byte-precise handling).
const POLL_INTERVAL_MS = 1000;

function readEntries(path: string): SessionLogEntry[] {
  try {
    return parseSessionLog(readFileSync(path, "utf-8"));
  } catch {
    return [];
  }
}

export function useSessionLog(path: string | null): SessionLogEntry[] {
  const [entries, setEntries] = useState<SessionLogEntry[]>(() =>
    path ? readEntries(path) : [],
  );

  useEffect(() => {
    if (!path) {
      setEntries([]);

      return;
    }

    setEntries(readEntries(path));
    const timer = setInterval(() => {
      setEntries(readEntries(path));
    }, POLL_INTERVAL_MS);

    return () => clearInterval(timer);
  }, [path]);

  return entries;
}

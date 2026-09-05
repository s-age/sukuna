function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/** Mirrors `presentation/tree_presenter.py::format_local_timestamp()`:
 * parse the stored UTC ISO string, convert to the viewer's local
 * timezone, and render as `YYYY-MM-DD HH:mm:ss` (24-hour). `toLocaleString()`
 * is locale-dependent and would not match that fixed format, so this
 * formats each field manually instead. */
export function formatLocalTimestamp(updatedAtUtcIso: string): string {
  const date = new Date(updatedAtUtcIso);
  const year = date.getFullYear();
  const month = pad(date.getMonth() + 1);
  const day = pad(date.getDate());
  const hours = pad(date.getHours());
  const minutes = pad(date.getMinutes());
  const seconds = pad(date.getSeconds());

  return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
}

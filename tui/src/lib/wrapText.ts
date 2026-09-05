import wrapAnsi from "wrap-ansi";

// SessionHistory is re-derived every poll tick (useSessionLog.ts polls the
// session log once a second), so recomputing wrap results for the entire
// history on every tick would make cost grow linearly with log length. Each
// entry's formatted text is immutable once written, so it's cached by text
// -- the cache itself must stay bounded.
const MAX_CACHE_ENTRIES = 2000;

let cache = new Map<string, string[]>();
let cachedWidth: number | null = null;

export function wrapLine(text: string, width: number): string[] {
  if (width !== cachedWidth) {
    // A terminal resize invalidates every cached wrap, not just some of
    // them -- a full clear is simpler and cheaper than partial invalidation.
    cache = new Map();
    cachedWidth = width;
  }

  const cached = cache.get(text);
  if (cached) return cached;

  const wrapped = wrapAnsi(text, Math.max(1, width), { trim: false, hard: true }).split("\n");

  if (cache.size >= MAX_CACHE_ENTRIES) {
    const oldestKey = cache.keys().next().value;
    if (oldestKey !== undefined) cache.delete(oldestKey);
  }
  cache.set(text, wrapped);

  return wrapped;
}

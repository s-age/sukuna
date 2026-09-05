import { describe, it, expect } from "vitest";
import { wrapLine } from "../wrapText.js";

describe("wrapLine", () => {
  it("returns the text unwrapped when it fits within the width", () => {
    expect(wrapLine("short line", 20)).toEqual(["short line"]);
  });

  it("wraps text longer than the width across multiple lines", () => {
    // trim: false (matching Ink's own wrap-text.js call) keeps each
    // line's trailing space instead of stripping it at the wrap point.
    expect(wrapLine("a bb ccc dddd", 5)).toEqual(["a bb ", "ccc ", "dddd"]);
  });

  it("hard-breaks a single word longer than the width", () => {
    expect(wrapLine("abcdefghij", 4)).toEqual(["abcd", "efgh", "ij"]);
  });

  it("returns the exact same array instance on a cache hit (not just an equal one)", () => {
    // Reference identity is the only way to prove a cache hit actually
    // happened -- a cache-free implementation would also satisfy toEqual().
    const first = wrapLine("cached text here", 6);
    const second = wrapLine("cached text here", 6);

    expect(second).toBe(first);
  });

  it("evicts the oldest cached entry once MAX_CACHE_ENTRIES (2000) is exceeded", () => {
    const firstText = "evict-me-first";
    const firstResult = wrapLine(firstText, 7);

    for (let i = 0; i < 2000; i += 1) wrapLine(`filler-${i}`, 7);

    expect(wrapLine(firstText, 7)).not.toBe(firstResult);
  });

  it("recomputes when the width changes, even for previously cached text", () => {
    const atWidthFive = wrapLine("width changes", 5);
    const atWidthTwenty = wrapLine("width changes", 20);

    expect(atWidthFive).not.toEqual(atWidthTwenty);
    expect(atWidthTwenty).toEqual(["width changes"]);
  });

  it("clamps a width below 1 to 1 instead of throwing", () => {
    expect(() => wrapLine("x", 0)).not.toThrow();
  });
});

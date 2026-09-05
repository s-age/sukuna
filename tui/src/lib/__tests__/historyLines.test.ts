import { describe, it, expect } from "vitest";
import { flattenEntriesToLines } from "../historyLines.js";
import type { SessionLogEntry } from "../sessionLog.js";

describe("flattenEntriesToLines", () => {
  it("formats a short entry as a single line with its role prefix", () => {
    const entries: SessionLogEntry[] = [{ role: "user", text: "hello" }];

    expect(flattenEntriesToLines(entries, 40)).toEqual([{ role: "user", text: "[user] hello" }]);
  });

  it("wraps a long entry into multiple lines, all tagged with the same role", () => {
    const entries: SessionLogEntry[] = [
      { role: "assistant", text: "one two three four five six seven eight" },
    ];

    const lines = flattenEntriesToLines(entries, 10);

    expect(lines.length).toBeGreaterThan(1);
    expect(lines.every((line) => line.role === "assistant")).toBe(true);
  });

  it("puts the [role] prefix only on the first line of a wrapped entry", () => {
    const entries: SessionLogEntry[] = [
      { role: "tool", text: "one two three four five six seven eight" },
    ];

    const lines = flattenEntriesToLines(entries, 10);

    expect(lines[0].text.startsWith("[tool]")).toBe(true);
    expect(lines.slice(1).some((line) => line.text.includes("[tool]"))).toBe(false);
  });

  it("normalizes embedded whitespace/newlines before wrapping, same as the old truncating summarize()", () => {
    const entries: SessionLogEntry[] = [{ role: "user", text: "line one\n\n  line two" }];

    expect(flattenEntriesToLines(entries, 40)).toEqual([
      { role: "user", text: "[user] line one line two" },
    ]);
  });

  it("flattens multiple entries into a single ordered array of lines", () => {
    const entries: SessionLogEntry[] = [
      { role: "user", text: "first" },
      { role: "assistant", text: "second" },
    ];

    expect(flattenEntriesToLines(entries, 40)).toEqual([
      { role: "user", text: "[user] first" },
      { role: "assistant", text: "[assistant] second" },
    ]);
  });

  it("returns an empty array for an empty entries list", () => {
    expect(flattenEntriesToLines([], 40)).toEqual([]);
  });
});

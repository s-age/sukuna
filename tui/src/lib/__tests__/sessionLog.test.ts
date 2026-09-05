import { describe, it, expect } from "vitest";
import { parseSessionLog, parseSessionLogEntry } from "../sessionLog.js";

describe("parseSessionLogEntry", () => {
  it("returns a user entry for a plain string message", () => {
    const line = JSON.stringify({ type: "user", message: { content: "hello there" } });

    expect(parseSessionLogEntry(line)).toEqual({ role: "user", text: "hello there" });
  });

  it("joins assistant text blocks and ignores thinking blocks", () => {
    const line = JSON.stringify({
      type: "assistant",
      message: {
        content: [
          { type: "thinking", thinking: "internal deliberation" },
          { type: "text", text: "here is the answer" },
        ],
      },
    });

    expect(parseSessionLogEntry(line)).toEqual({
      role: "assistant",
      text: "here is the answer",
    });
  });

  it("returns null for a tool_use-only assistant entry (the '[tool] ...' line already shows this)", () => {
    const line = JSON.stringify({
      type: "assistant",
      message: { content: [{ type: "tool_use", name: "Read", input: {} }] },
    });

    expect(parseSessionLogEntry(line)).toBeNull();
  });

  it("keeps the text and drops the tool_use when an assistant entry has both", () => {
    const line = JSON.stringify({
      type: "assistant",
      message: {
        content: [
          { type: "text", text: "checking the file" },
          { type: "tool_use", name: "Read", input: {} },
        ],
      },
    });

    expect(parseSessionLogEntry(line)).toEqual({
      role: "assistant",
      text: "checking the file",
    });
  });

  it("labels a tool-result-only user entry as role 'tool'", () => {
    const line = JSON.stringify({
      type: "user",
      message: { content: [{ type: "tool_result", tool_use_id: "x", content: [] }] },
    });

    expect(parseSessionLogEntry(line)).toEqual({ role: "tool", text: "[tool result]" });
  });

  it("returns null for entries with no displayable content", () => {
    const line = JSON.stringify({
      type: "assistant",
      message: { content: [{ type: "thinking", thinking: "" }] },
    });

    expect(parseSessionLogEntry(line)).toBeNull();
  });

  it("returns null for non-user/assistant entry types", () => {
    const line = JSON.stringify({ type: "custom-title", customTitle: "ccw-x-1" });

    expect(parseSessionLogEntry(line)).toBeNull();
  });

  it("returns null for malformed JSON", () => {
    expect(parseSessionLogEntry("not json")).toBeNull();
  });

  it("returns null for a blank line", () => {
    expect(parseSessionLogEntry("   ")).toBeNull();
  });
});

describe("parseSessionLog", () => {
  it("parses multiple lines, skipping ones with no displayable entry", () => {
    const raw = [
      JSON.stringify({ type: "user", message: { content: "first" } }),
      JSON.stringify({ type: "custom-title", customTitle: "ccw-x-1" }),
      "not json",
      JSON.stringify({
        type: "assistant",
        message: { content: [{ type: "text", text: "second" }] },
      }),
    ].join("\n");

    expect(parseSessionLog(raw)).toEqual([
      { role: "user", text: "first" },
      { role: "assistant", text: "second" },
    ]);
  });

  function toolUseLine(id: string, name: string, input: unknown): string {
    return JSON.stringify({
      type: "assistant",
      message: { content: [{ type: "tool_use", id, name, input }] },
    });
  }

  function toolResultLine(toolUseId: string): string {
    return JSON.stringify({
      type: "user",
      message: { content: [{ type: "tool_result", tool_use_id: toolUseId, content: [] }] },
    });
  }

  function truncateForTest(value: string): string {
    return value.length > 60 ? `${value.slice(0, 60)}…` : value;
  }

  it("correlates a tool_result with its tool_use and shows name + primary arg", () => {
    const raw = [toolUseLine("t1", "Bash", { command: "git status" }), toolResultLine("t1")].join(
      "\n",
    );

    const entries = parseSessionLog(raw);

    expect(entries[0]).toEqual({ role: "tool", text: "Bash(git status) result" });
  });

  it("shows only the basename for path-style primary arguments", () => {
    const raw = [
      toolUseLine("t1", "Read", { file_path: "/Users/s-age/repo/tui/src/lib/sessionLog.ts" }),
      toolResultLine("t1"),
    ].join("\n");

    const entries = parseSessionLog(raw);

    expect(entries[0]).toEqual({ role: "tool", text: "Read(sessionLog.ts) result" });
  });

  it("truncates a primary argument longer than 60 characters with an ellipsis", () => {
    const longCommand = "echo " + "x".repeat(60);
    const raw = [toolUseLine("t1", "Bash", { command: longCommand }), toolResultLine("t1")].join(
      "\n",
    );

    const entries = parseSessionLog(raw);
    const expectedArg = `${longCommand.slice(0, 60)}…`;

    expect(entries[0]).toEqual({ role: "tool", text: `Bash(${expectedArg}) result` });
  });

  it("falls back to a truncated JSON stringify when no primary key matches", () => {
    const raw = [
      toolUseLine("t1", "TodoWrite", { todos: [{ content: "a" }, { content: "b" }] }),
      toolResultLine("t1"),
    ].join("\n");

    const entries = parseSessionLog(raw);
    const expectedArg = truncateForTest(JSON.stringify({ todos: [{ content: "a" }, { content: "b" }] }));

    expect(entries[0]).toEqual({ role: "tool", text: `TodoWrite(${expectedArg}) result` });
  });

  it("falls back to the fixed placeholder when the tool_use_id has no correlation", () => {
    const raw = toolResultLine("missing");

    const entries = parseSessionLog(raw);

    expect(entries[0]).toEqual({ role: "tool", text: "[tool result]" });
  });
});

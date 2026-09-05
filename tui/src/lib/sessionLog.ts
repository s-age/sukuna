// Pure parsing of a Claude Code session transcript (jsonl) into a short
// display line per turn. No filesystem access here -- see
// `useSessionLog.ts` for the polling read that feeds this.

export type SessionLogEntry = {
  role: "user" | "assistant" | "tool";
  text: string;
};

type ContentBlock = {
  type?: string;
  text?: string;
  name?: string;
  id?: string;
  input?: unknown;
  tool_use_id?: string;
};

type EntryRole = "user" | "assistant";

type RawEntry = {
  type: EntryRole;
  message?: { content?: unknown };
};

type ToolUseInfo = { name: string; input: unknown };

// Primary-argument key priority, tool-agnostic. Kept as a flat list
// rather than a per-tool switch because the vocabulary (`command` for
// Bash, `pattern` for Grep/Glob, ...) doesn't collide across
// Anthropic's built-in tools or the MCP tools observed in this repo.
const PRIMARY_ARG_KEYS = [
  "command",
  "file_path",
  "pattern",
  "query",
  "url",
  "description",
  "path",
  "notebook_path",
  "prompt",
  "cardID",
] as const;

const PATH_ARG_KEYS = new Set<string>(["file_path", "path", "notebook_path"]);

const MAX_ARG_SUMMARY_LENGTH = 60;

function isToolResultOnly(blocks: ContentBlock[]): boolean {
  return blocks.length > 0 && blocks.every((block) => block.type === "tool_result");
}

function collectText(blocks: ContentBlock[]): string {
  const texts: string[] = [];

  for (const block of blocks) {
    if (block.type === "text" && typeof block.text === "string") {
      texts.push(block.text);
    }
  }

  return texts.join("\n").trim();
}

function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function basename(value: string): string {
  return value.split("/").pop() || value;
}

function summarizeToolInput(input: unknown): string {
  if (typeof input !== "object" || input === null) return "";
  const obj = input as Record<string, unknown>;
  if (Object.keys(obj).length === 0) return "";

  for (const key of PRIMARY_ARG_KEYS) {
    const value = obj[key];
    if (typeof value !== "string") continue;
    return PATH_ARG_KEYS.has(key)
      ? basename(value)
      : truncate(normalizeWhitespace(value), MAX_ARG_SUMMARY_LENGTH);
  }

  return truncate(JSON.stringify(obj), MAX_ARG_SUMMARY_LENGTH);
}

function formatToolResult(toolName: string, argSummary: string): string {
  return argSummary ? `${toolName}(${argSummary}) result` : `${toolName} result`;
}

function tryParseRawEntry(rawLine: string): RawEntry | null {
  const trimmed = rawLine.trim();
  if (!trimmed) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;

  const candidate = parsed as { type?: string; message?: { content?: unknown } };
  if (candidate.type !== "user" && candidate.type !== "assistant") return null;

  return { type: candidate.type, message: candidate.message };
}

function buildEntry(
  entry: RawEntry,
  toolUseById?: ReadonlyMap<string, ToolUseInfo>,
): SessionLogEntry | null {
  const content = entry.message?.content;
  if (typeof content === "string") {
    const text = content.trim();
    return text ? { role: entry.type, text } : null;
  }
  if (!Array.isArray(content)) return null;

  const blocks = content as ContentBlock[];
  if (entry.type === "user" && isToolResultOnly(blocks)) {
    const toolUseId = blocks[0]?.tool_use_id;
    const toolUse = typeof toolUseId === "string" ? toolUseById?.get(toolUseId) : undefined;
    if (!toolUse) return { role: "tool", text: "[tool result]" };
    return { role: "tool", text: formatToolResult(toolUse.name, summarizeToolInput(toolUse.input)) };
  }

  const text = collectText(blocks);
  return text ? { role: entry.type, text } : null;
}

export function parseSessionLogEntry(rawLine: string): SessionLogEntry | null {
  const entry = tryParseRawEntry(rawLine);
  return entry ? buildEntry(entry) : null;
}

export function parseSessionLog(raw: string): SessionLogEntry[] {
  const entries: SessionLogEntry[] = [];
  const toolUseById = new Map<string, ToolUseInfo>();

  for (const line of raw.split("\n")) {
    const entry = tryParseRawEntry(line);
    if (!entry) continue;

    const content = entry.message?.content;
    if (entry.type === "assistant" && Array.isArray(content)) {
      for (const block of content as ContentBlock[]) {
        if (block.type === "tool_use" && typeof block.id === "string" && typeof block.name === "string") {
          toolUseById.set(block.id, { name: block.name, input: block.input });
        }
      }
    }

    const built = buildEntry(entry, toolUseById);
    if (built) entries.push(built);
  }

  return entries;
}

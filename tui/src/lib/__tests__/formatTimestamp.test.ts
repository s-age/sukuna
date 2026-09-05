process.env.TZ = "UTC";

import { describe, it, expect } from "vitest";
import { formatLocalTimestamp } from "../formatTimestamp.js";

describe("formatLocalTimestamp", () => {
  it("renders a UTC ISO timestamp as YYYY-MM-DD HH:mm:ss in the local timezone", () => {
    expect(formatLocalTimestamp("2026-08-28T13:45:07.000Z")).toBe("2026-08-28 13:45:07");
  });

  it("zero-pads single-digit month, day, hour, minute, and second", () => {
    expect(formatLocalTimestamp("2026-01-02T03:04:05.000Z")).toBe("2026-01-02 03:04:05");
  });
});

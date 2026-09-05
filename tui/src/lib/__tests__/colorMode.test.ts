import { describe, it, expect } from "vitest";
import { parseOsc11Response, relativeLuminance, pickForegroundColor } from "../colorMode.js";

describe("parseOsc11Response", () => {
  it("parses a 4-digit-per-channel BEL-terminated response", () => {
    expect(parseOsc11Response("\x1b]11;rgb:0000/0000/0000\x07")).toEqual({
      r: 0,
      g: 0,
      b: 0,
    });
  });

  it("parses a 4-digit-per-channel ST-terminated response", () => {
    expect(parseOsc11Response("\x1b]11;rgb:ffff/ffff/ffff\x1b\\")).toEqual({
      r: 255,
      g: 255,
      b: 255,
    });
  });

  it("parses a 2-digit-per-channel response", () => {
    expect(parseOsc11Response("\x1b]11;rgb:80/40/20\x07")).toEqual({
      r: 128,
      g: 64,
      b: 32,
    });
  });

  it("finds the response even when preceded by stray bytes", () => {
    expect(parseOsc11Response("garbage\x1b]11;rgb:1234/5678/9abc\x07")).toEqual({
      r: 18,
      g: 86,
      b: 154,
    });
  });

  it("scales a 1-digit-per-channel response to the full 0-255 range, not a zero-padded byte", () => {
    expect(parseOsc11Response("\x1b]11;rgb:f/f/f\x07")).toEqual({
      r: 255,
      g: 255,
      b: 255,
    });
  });

  it("scales a 1-digit-per-channel partial value proportionally", () => {
    expect(parseOsc11Response("\x1b]11;rgb:8/8/8\x07")).toEqual({
      r: 136,
      g: 136,
      b: 136,
    });
  });

  it("scales a 3-digit-per-channel response proportionally", () => {
    expect(parseOsc11Response("\x1b]11;rgb:800/400/200\x07")).toEqual({
      r: 128,
      g: 64,
      b: 32,
    });
  });

  it("returns null for an empty or unrelated chunk", () => {
    expect(parseOsc11Response("")).toBeNull();
    expect(parseOsc11Response("hello world")).toBeNull();
  });

  it("returns null for a partial response still awaiting more bytes", () => {
    expect(parseOsc11Response("\x1b]11;rgb:ffff/ff")).toBeNull();
  });
});

describe("relativeLuminance", () => {
  it("is 0 for black", () => {
    expect(relativeLuminance({ r: 0, g: 0, b: 0 })).toBe(0);
  });

  it("is 1 for white", () => {
    expect(relativeLuminance({ r: 255, g: 255, b: 255 })).toBe(1);
  });

  it("weights green the most and blue the least", () => {
    const red = relativeLuminance({ r: 255, g: 0, b: 0 });
    const green = relativeLuminance({ r: 0, g: 255, b: 0 });
    const blue = relativeLuminance({ r: 0, g: 0, b: 255 });

    expect(green).toBeGreaterThan(red);
    expect(red).toBeGreaterThan(blue);
  });
});

describe("pickForegroundColor", () => {
  it("picks white text for a dark (low luminance) background", () => {
    expect(pickForegroundColor(0)).toBe("white");
    expect(pickForegroundColor(0.49)).toBe("white");
  });

  it("picks black text for a light (high luminance) background", () => {
    expect(pickForegroundColor(0.5)).toBe("black");
    expect(pickForegroundColor(1)).toBe("black");
  });
});

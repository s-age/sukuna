import { describe, it, expect } from "vitest";
import {
  isFocusToggle,
  isQuit,
  resolveFocusTarget,
  resolveMoveDelta,
  resolvePageDirection,
} from "./useKeyHandler.js";
import type { Key } from "ink";

function key(overrides: Partial<Key> = {}): Key {
  return {
    upArrow: false,
    downArrow: false,
    leftArrow: false,
    rightArrow: false,
    pageDown: false,
    pageUp: false,
    home: false,
    end: false,
    return: false,
    escape: false,
    ctrl: false,
    shift: false,
    tab: false,
    backspace: false,
    delete: false,
    meta: false,
    super: false,
    hyper: false,
    capsLock: false,
    numLock: false,
    ...overrides,
  };
}

describe("isFocusToggle", () => {
  it("is true when Tab is pressed", () => {
    expect(isFocusToggle("", key({ tab: true }))).toBe(true);
  });

  it("is also true for Shift+Tab -- a two-pane toggle has no reverse direction", () => {
    expect(isFocusToggle("", key({ tab: true, shift: true }))).toBe(true);
  });

  it("is false for an unrelated key", () => {
    expect(isFocusToggle("j", key())).toBe(false);
  });
});

describe("resolveFocusTarget", () => {
  it("resolves left arrow to the left pane", () => {
    expect(resolveFocusTarget(key({ leftArrow: true }))).toBe("left");
  });

  it("resolves right arrow to the history pane", () => {
    expect(resolveFocusTarget(key({ rightArrow: true }))).toBe("history");
  });

  it("is null for an unrelated key", () => {
    expect(resolveFocusTarget(key())).toBeNull();
  });
});

describe("resolveMoveDelta", () => {
  it("still resolves j/k and arrow keys unaffected by the Tab addition", () => {
    expect(resolveMoveDelta("j", key())).toBe(1);
    expect(resolveMoveDelta("k", key())).toBe(-1);
    expect(resolveMoveDelta("x", key())).toBeNull();
  });

  it("still resolves j/k unaffected by the left/right arrow focus addition", () => {
    expect(resolveMoveDelta("j", key({ leftArrow: true }))).toBe(1);
    expect(resolveMoveDelta("k", key({ rightArrow: true }))).toBe(-1);
  });
});

describe("resolvePageDirection", () => {
  it("resolves Page Up to -1", () => {
    expect(resolvePageDirection(key({ pageUp: true }))).toBe(-1);
  });

  it("resolves Page Down to 1", () => {
    expect(resolvePageDirection(key({ pageDown: true }))).toBe(1);
  });

  it("is null for an unrelated key", () => {
    expect(resolvePageDirection(key())).toBeNull();
  });
});

describe("isQuit", () => {
  it("still resolves q and Ctrl-C unaffected by the Tab addition", () => {
    expect(isQuit("q", key())).toBe(true);
    expect(isQuit("c", key({ ctrl: true }))).toBe(true);
    expect(isQuit("x", key())).toBe(false);
  });
});

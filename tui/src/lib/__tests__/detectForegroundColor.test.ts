import { EventEmitter } from "node:events";
import { describe, it, expect, vi } from "vitest";
import { detectForegroundColor } from "../detectForegroundColor.js";
import type { DetectStdin, DetectStdout } from "../detectForegroundColor.js";

class FakeStdin extends EventEmitter implements DetectStdin {
  isTTY = true;
  isRaw = false;
  setRawMode = vi.fn((mode: boolean) => {
    this.isRaw = mode;
  });

  resume = vi.fn();
  pause = vi.fn();

  removeListener(event: "data", listener: (chunk: Buffer | string) => void): this {
    return super.removeListener(event, listener);
  }
}

class FakeStdout implements DetectStdout {
  isTTY = true;
  written: string[] = [];

  write = (chunk: string): void => {
    this.written.push(chunk);
  };
}

describe("detectForegroundColor", () => {
  it("resolves undefined without writing a query when stdin is not a TTY", async () => {
    const stdin = new FakeStdin();
    stdin.isTTY = false;
    const stdout = new FakeStdout();

    const result = await detectForegroundColor({ stdin, stdout, timeoutMs: 20 });

    expect(result).toBeUndefined();
    expect(stdout.written).toEqual([]);
  });

  it("resolves undefined without writing a query when stdout is not a TTY", async () => {
    const stdin = new FakeStdin();
    const stdout = new FakeStdout();
    stdout.isTTY = false;

    const result = await detectForegroundColor({ stdin, stdout, timeoutMs: 20 });

    expect(result).toBeUndefined();
    expect(stdout.written).toEqual([]);
  });

  it("queries the terminal, resolves from the OSC 11 response, and restores raw mode", async () => {
    const stdin = new FakeStdin();
    const stdout = new FakeStdout();

    const pending = detectForegroundColor({ stdin, stdout, timeoutMs: 1000 });

    expect(stdout.written.join("")).toContain("]11;?");
    expect(stdin.setRawMode).toHaveBeenCalledWith(true);

    stdin.emit("data", Buffer.from("\x1b]11;rgb:0000/0000/0000\x07"));

    await expect(pending).resolves.toBe("white");
    expect(stdin.setRawMode).toHaveBeenLastCalledWith(false);
    expect(stdin.pause).toHaveBeenCalled();
  });

  it("resolves black text for a light background response", async () => {
    const stdin = new FakeStdin();
    const stdout = new FakeStdout();

    const pending = detectForegroundColor({ stdin, stdout, timeoutMs: 1000 });
    stdin.emit("data", Buffer.from("\x1b]11;rgb:ffff/ffff/ffff\x07"));

    await expect(pending).resolves.toBe("black");
  });

  it("assembles a response split across multiple data events", async () => {
    const stdin = new FakeStdin();
    const stdout = new FakeStdout();

    const pending = detectForegroundColor({ stdin, stdout, timeoutMs: 1000 });
    stdin.emit("data", Buffer.from("\x1b]11;rgb:0000/"));
    stdin.emit("data", Buffer.from("0000/0000\x07"));

    await expect(pending).resolves.toBe("white");
  });

  it("resolves undefined and restores raw mode when no response arrives before the timeout", async () => {
    const stdin = new FakeStdin();
    const stdout = new FakeStdout();

    const result = await detectForegroundColor({ stdin, stdout, timeoutMs: 10 });

    expect(result).toBeUndefined();
    expect(stdin.setRawMode).toHaveBeenLastCalledWith(false);
    expect(stdin.pause).toHaveBeenCalled();
  });

  it("does not re-pause a stdin that was already in raw mode before detection", async () => {
    const stdin = new FakeStdin();
    stdin.isRaw = true;
    const stdout = new FakeStdout();

    await detectForegroundColor({ stdin, stdout, timeoutMs: 10 });

    expect(stdin.setRawMode).toHaveBeenLastCalledWith(true);
    expect(stdin.pause).not.toHaveBeenCalled();
  });
});

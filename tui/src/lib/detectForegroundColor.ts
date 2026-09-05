import {
  parseOsc11Response,
  pickForegroundColor,
  relativeLuminance,
  type ForegroundColor,
} from "./colorMode.js";

const OSC11_QUERY = "\x1b]11;?\x07";
const DEFAULT_TIMEOUT_MS = 300;

export type DetectStdin = {
  readonly isTTY?: boolean;
  isRaw?: boolean;
  setRawMode: (mode: boolean) => void;
  resume: () => void;
  pause: () => void;
  on: (event: "data", listener: (chunk: Buffer | string) => void) => void;
  removeListener: (event: "data", listener: (chunk: Buffer | string) => void) => void;
};

export type DetectStdout = {
  readonly isTTY?: boolean;
  write: (chunk: string) => void;
};

export type DetectOptions = {
  stdin?: DetectStdin;
  stdout?: DetectStdout;
  timeoutMs?: number;
};

// Must resolve before Ink's render() claims stdin; resolves undefined (no override) on non-TTY/unparseable/timeout, matching the terminal-default fallback spec.
export async function detectForegroundColor(
  options: DetectOptions = {},
): Promise<ForegroundColor | undefined> {
  const stdin = options.stdin ?? (process.stdin as unknown as DetectStdin);
  const stdout = options.stdout ?? (process.stdout as unknown as DetectStdout);
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  if (!stdin.isTTY || !stdout.isTTY || typeof stdin.setRawMode !== "function") {
    return undefined;
  }

  return new Promise((resolve) => {
    const wasRaw = stdin.isRaw ?? false;
    let buffer = "";
    let settled = false;

    const finish = (result: ForegroundColor | undefined): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      stdin.removeListener("data", onData);
      stdin.setRawMode(wasRaw);
      if (!wasRaw) stdin.pause();
      resolve(result);
    };

    const onData = (chunk: Buffer | string): void => {
      buffer += chunk.toString();
      const rgb = parseOsc11Response(buffer);
      if (rgb === null) return;
      finish(pickForegroundColor(relativeLuminance(rgb)));
    };

    const timer = setTimeout(() => finish(undefined), timeoutMs);

    stdin.setRawMode(true);
    stdin.resume();
    stdin.on("data", onData);
    stdout.write(OSC11_QUERY);
  });
}

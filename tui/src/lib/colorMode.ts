export type ForegroundColor = "white" | "black";

export type RgbColor = {
  r: number;
  g: number;
  b: number;
};

const OSC11_RESPONSE_PATTERN = /]11;rgb:([0-9a-f]{1,4})\/([0-9a-f]{1,4})\/([0-9a-f]{1,4})/i;

export const LUMINANCE_THRESHOLD = 0.5;

function parseColorComponent(hex: string): number {
  const value = parseInt(hex, 16);
  const maxValue = 16 ** hex.length - 1;

  return Math.round((value / maxValue) * 255);
}

export function parseOsc11Response(chunk: string): RgbColor | null {
  const match = OSC11_RESPONSE_PATTERN.exec(chunk);
  if (!match) return null;

  const [, rHex, gHex, bHex] = match;

  return {
    r: parseColorComponent(rHex),
    g: parseColorComponent(gHex),
    b: parseColorComponent(bHex),
  };
}

export function relativeLuminance({ r, g, b }: RgbColor): number {
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255;
}

export function pickForegroundColor(luminance: number): ForegroundColor {
  return luminance >= LUMINANCE_THRESHOLD ? "black" : "white";
}

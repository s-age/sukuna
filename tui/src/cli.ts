import { readFileSync } from "node:fs";
import React from "react";
import { render } from "ink";
import { App } from "./App.js";
import { ForegroundColorProvider } from "./ForegroundColorContext.js";
import { detectForegroundColor } from "./lib/detectForegroundColor.js";
import type { TreePayload } from "./types.js";

async function main(): Promise<void> {
  const payloadPath = process.argv[2];
  if (!payloadPath) {
    process.stderr.write("usage: tree-tui.mjs <payload-json-path>\n");
    process.exitCode = 2;

    return;
  }

  const raw = readFileSync(payloadPath, "utf-8");
  const payload = JSON.parse(raw) as TreePayload;
  const foregroundColor = await detectForegroundColor();
  const app = render(
    React.createElement(ForegroundColorProvider, {
      value: foregroundColor,
      children: React.createElement(App, { payload }),
    }),
  );

  app.waitUntilExit().then(() => {
    process.exit(0);
  });
}

void main();

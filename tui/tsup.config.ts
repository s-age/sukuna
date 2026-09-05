import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "tsup";

const dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  entry: { "tree-tui": "src/cli.ts" },
  format: ["esm"],
  platform: "node",
  target: "node24",
  bundle: true,
  splitting: false,
  outExtension: () => ({ js: ".mjs" }),
  noExternal: [/.*/],
  clean: true,
  // esbuild's CJS->ESM interop shim throws "Dynamic require ... is not
  // supported" at runtime for dependencies (e.g. signal-exit) that
  // `require()` a Node builtin -- ESM has no ambient `require`. Without
  // this banner the bundle only fails once copied to a directory with no
  // adjacent node_modules (exactly the distribution shape this bundle is
  // for), not in a `tui/` dev checkout.
  banner: {
    js: "import { createRequire as __sukunaCreateRequire } from 'node:module'; const require = __sukunaCreateRequire(import.meta.url);",
  },
  esbuildOptions(options) {
    // ink's optional DevTools bridge statically imports react-devtools-core,
    // an optional peer dependency this project never installs. It's only
    // ever reached behind `process.env.DEV === 'true'`, but esbuild still
    // needs to resolve the import to produce a single-file bundle, so
    // point it at a no-op stub instead of leaving it external (external
    // would need a real node_modules/react-devtools-core at runtime).
    options.alias = {
      ...options.alias,
      "react-devtools-core": path.join(dirname, "src/stubs/react-devtools-core.ts"),
    };
  },
});

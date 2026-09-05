#!/usr/bin/env bash
# Build the tree TUI's Node.js bundle and copy it into the Python package's
# package-data directory. Not run automatically by `python -m build` --
# setuptools does not fail when a package-data glob has no match, so a
# wheel built without running this first is missing `--tui` support at
# runtime (see the development/verification command list in CLAUDE.md).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

(cd "$repo_root/tui" && npm ci && npm run build)

mkdir -p "$repo_root/src/sukuna/_tui_bundle"
cp "$repo_root/tui/dist/tree-tui.mjs" "$repo_root/src/sukuna/_tui_bundle/tree-tui.mjs"

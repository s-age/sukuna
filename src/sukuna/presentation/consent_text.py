"""`sukuna-cli init`'s consent explanation text (presentation, not usecase --
`init()` never references this; it is display-only content for
`cli_human.py`'s `_prompt_install_hooks` and `_prompt_enable_tui`."""

from __future__ import annotations

CONSENT_TEXT = """\
sukuna-cli init will add one entry to your user-level Claude Code hook \
configuration (~/.claude/settings.json, PreToolUse), so that sukuna's \
`focus` command runs automatically instead of needing a manual call:

  An AskUserQuestion hook that runs `sukuna focus` (no --worker), \
resizing the asking session's own pane and switching your terminal's \
real active pane/session to it -- tmux's active pane, or iTerm2's active \
session -- keyboard input follows -- since self-focus exists to pull the \
user's attention back to the pane asking the question. If the asking \
pane is already the active one, this is a silent, successful no-op: \
nothing needs to move.

The hook command is wrapped to always exit 0 (fail-open): if `sukuna \
focus` errors for any reason, the AskUserQuestion tool call is never \
blocked by it. This only protects the failure path. It does NOT \
protect the success path: on a tmux session, any Claude Code session that \
has this hook installed -- not only sukuna-managed ones -- has its pane \
resolved on every AskUserQuestion call, because pane resolution reads \
$TMUX_PANE unconditionally. sukuna narrows which of those sessions can \
actually be resized/activated to ones it recognizes as sukuna-related: a \
session that has spawned a sukuna worker of its own, or a session whose \
own pane is itself a live sukuna worker's pane. Every other session gets \
a silent, successful no-op.

This installer only merges this hook entry into your existing \
~/.claude/settings.json, preserving every other key and every other hook \
you already have. It makes no other changes.
"""

TUI_CONSENT_TEXT = (
    "`sukuna-cli tree` launches an interactive terminal UI written in "
    "Node.js (Ink/React), bundled inside the sukuna package. Opting in here "
    "makes the TUI `sukuna-cli tree`'s DEFAULT output whenever the terminal "
    "is interactive and a compatible Node.js is found -- `--tui` still "
    "forces it (erroring instead of falling back if those requirements "
    "aren't met), and the new `--text` flag always forces the old static "
    "text output regardless of this setting. Answering no keeps text as "
    "the default (`--tui` still works on demand); pressing Enter at this "
    "prompt opts in, same as answering yes. "
    "Opting in lets `sukuna-cli init` check your Node.js version now; the "
    "actual launch still re-checks Node.js at invocation time (your "
    "environment may change between `init` and later use). The TUI reads "
    "the current worker tree from a temporary JSON file sukuna writes and "
    "deletes; it makes no network calls and writes nothing to the registry."
)

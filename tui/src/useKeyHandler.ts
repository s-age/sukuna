import { useInput } from "ink";
import type { Key } from "ink";

// The two focusable panes: worker tree on the left, session history
// bottom-right -- WorkerDetail top-right is never focusable.
export type FocusTarget = "left" | "history";

export type KeyHandlers = {
  onMove: (delta: number) => void;
  onPageMove: (direction: 1 | -1) => void;
  onQuit: () => void;
  onToggleFocus: () => void;
  onFocusTo: (target: FocusTarget) => void;
};

export function resolveMoveDelta(input: string, key: Key): number | null {
  if (key.upArrow || input === "k") return -1;
  if (key.downArrow || input === "j") return 1;

  return null;
}

// Ink's Key type already exposes pageUp/pageDown booleans (populated
// from terminal escape sequences distinct from the plain arrow keys),
// so this needs no new key parsing -- only a resolver in the same
// shape as resolveMoveDelta.
export function resolvePageDirection(key: Key): 1 | -1 | null {
  if (key.pageUp) return -1;
  if (key.pageDown) return 1;

  return null;
}

export function isQuit(input: string, key: Key): boolean {
  return input === "q" || (key.ctrl && input === "c");
}

// Only two panes are focusable, so a plain toggle is enough -- no
// Shift+Tab-style reverse direction is needed. `key.tab` is true for
// Shift+Tab too (`shift` is a separate flag), which toggling either
// way handles correctly without checking `key.shift`.
export function isFocusToggle(_input: string, key: Key): boolean {
  return key.tab;
}

// Absolute (not relative/toggle) focus targeting -- left arrow always
// means the left pane, right arrow always means history, independent
// of current focus. Pressing the arrow for the pane already focused
// re-sets the same target, which is a no-op by construction.
export function resolveFocusTarget(key: Key): FocusTarget | null {
  if (key.leftArrow) return "left";
  if (key.rightArrow) return "history";

  return null;
}

// Dispatch order is quit -> Tab -> left/right arrow (absolute focus)
// -> pageUp/pageDown (page move) -> up/down arrow or j/k (move).
// pageUp/pageDown are separate Key flags from the plain arrow keys, so
// this insertion point doesn't collide with any existing branch.
export function useKeyHandler(handlers: KeyHandlers): void {
  useInput((input, key) => {
    if (isQuit(input, key)) {
      handlers.onQuit();

      return;
    }

    if (isFocusToggle(input, key)) {
      handlers.onToggleFocus();

      return;
    }

    const focusTarget = resolveFocusTarget(key);
    if (focusTarget !== null) {
      handlers.onFocusTo(focusTarget);

      return;
    }

    const pageDirection = resolvePageDirection(key);
    if (pageDirection !== null) {
      handlers.onPageMove(pageDirection);

      return;
    }

    const delta = resolveMoveDelta(input, key);
    if (delta !== null) handlers.onMove(delta);
  });
}

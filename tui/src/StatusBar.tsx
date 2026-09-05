import React from "react";
import { Text } from "ink";
import { useForegroundColor } from "./ForegroundColorContext.js";

type StatusBarProps = {
  hiddenParentGroups: number;
};

export function StatusBar({ hiddenParentGroups }: StatusBarProps): React.JSX.Element {
  const foregroundColor = useForegroundColor();
  const hiddenNote =
    hiddenParentGroups > 0 ? `  (+${hiddenParentGroups} more parent groups)` : "";

  return (
    <Text color={foregroundColor} dimColor>
      ↑/↓ or j/k: move   Tab / ←/→: focus   q: quit{hiddenNote}
    </Text>
  );
}

import React, { createContext, useContext } from "react";
import type { ForegroundColor } from "./lib/colorMode.js";

const ForegroundColorContext = createContext<ForegroundColor | undefined>(undefined);

type ForegroundColorProviderProps = {
  value: ForegroundColor | undefined;
  children: React.ReactNode;
};

export function ForegroundColorProvider({
  value,
  children,
}: ForegroundColorProviderProps): React.JSX.Element {
  return (
    <ForegroundColorContext.Provider value={value}>{children}</ForegroundColorContext.Provider>
  );
}

export function useForegroundColor(): ForegroundColor | undefined {
  return useContext(ForegroundColorContext);
}

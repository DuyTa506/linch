import { createContext, useContext } from "react";

import { en, type Dictionary } from "./en";
import { vi } from "./vi";

export type Lang = "en" | "vi";

export const dictionaries: Record<Lang, Dictionary> = { en, vi };

export const I18nContext = createContext<Dictionary>(en);

/** The active dictionary. Copy is never inlined in components. */
export function useT(): Dictionary {
  return useContext(I18nContext);
}

export type { Dictionary };

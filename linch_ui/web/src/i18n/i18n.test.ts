import { describe, expect, it } from "vitest";

import { en } from "./en";
import { vi } from "./vi";

describe("i18n dictionaries", () => {
  it("define exactly the same keys", () => {
    expect(Object.keys(vi).sort()).toEqual(Object.keys(en).sort());
  });

  it("agree on the type of every entry", () => {
    for (const key of Object.keys(en) as (keyof typeof en)[]) {
      expect(typeof vi[key]).toBe(typeof en[key]);
    }
  });

  it("leaves no Vietnamese entry empty", () => {
    for (const [key, value] of Object.entries(vi)) {
      if (typeof value === "string") {
        expect(value.trim(), `vi.${key} is empty`).not.toBe("");
      }
    }
  });

  it("translates rather than copying English prose", () => {
    // Proper nouns and CLI-ish tokens are expected to match; prose should not.
    const shared = [
      "brand",
      "local",
      "colId",
      "colModel",
      "canvas",
      "tabYaml",
      "tabFiles",
      "aiAssist",
      "yamlFile", // a literal filename, not prose
    ];
    const identical = (Object.keys(en) as (keyof typeof en)[]).filter(
      (key) =>
        typeof en[key] === "string" &&
        typeof vi[key] === "string" &&
        en[key] === vi[key] &&
        !shared.includes(key as string),
    );
    expect(identical).toEqual([]);
  });
});

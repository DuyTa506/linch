import { stringify } from "yaml";

import type { Blueprint } from "../api/types";

/**
 * Recursively drop `null`/`undefined` members.
 *
 * Safe for the digest: `canonical_data` dumps the *parsed* model with
 * `exclude_none=False`, so a field left out of the YAML and a field written as
 * its default parse to the same model and therefore the same digest. Pruning is
 * purely for readability of the YAML the user sees.
 */
export function pruneEmpty<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((item) => pruneEmpty(item)) as unknown as T;
  }
  if (value !== null && typeof value === "object") {
    const source = value as Record<string, unknown>;
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(source)) {
      if (item === null || item === undefined) continue;
      result[key] = pruneEmpty(item);
    }
    return result as unknown as T;
  }
  return value;
}

/**
 * Serialize a Blueprint back to YAML for `PUT /blueprint`.
 *
 * The input is the server's own `ProjectDocument.blueprint` JSON (already
 * camelCase, already schema-shaped), so this carries no copy of the Pydantic
 * spec — it re-emits what the server produced. The server remains the
 * authority: an invalid emit is rejected with `blueprint.structural_invalid`
 * and is never persisted.
 *
 * Comments in the user's YAML do not survive this round-trip.
 */
export function emitBlueprintYaml(blueprint: Blueprint): string {
  return stringify(pruneEmpty(blueprint), {
    indent: 2,
    lineWidth: 0,
    nullStr: "null",
  });
}

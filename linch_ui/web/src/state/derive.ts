import type { Diagnostic, StatusId } from "../api/types";

export type SaveState = "saved" | "draft" | "buffer" | "conflict" | "new" | "saving";

export type Marker = "[ok]" | "[!!]" | "[xx]" | "[--]";

/** The design's universal marker vocabulary. */
export function markerColor(marker: Marker): string {
  switch (marker) {
    case "[xx]":
      return "var(--red)";
    case "[!!]":
      return "var(--yel)";
    case "[--]":
      return "var(--g6)";
    default:
      return "var(--ink)";
  }
}

export function severityMarker(severity: Diagnostic["severity"]): Marker {
  if (severity === "error") return "[xx]";
  if (severity === "warning") return "[!!]";
  return "[ok]";
}

export function countBy(diagnostics: Diagnostic[], severity: Diagnostic["severity"]): number {
  return diagnostics.filter((item) => item.severity === severity).length;
}

export function hasErrors(diagnostics: Diagnostic[]): boolean {
  return countBy(diagnostics, "error") > 0;
}

/** Short digest for the top bar: `9b2e…4c` */
export function shortDigest(digest: string): string {
  if (digest.length < 8) return digest;
  return `${digest.slice(0, 4)}…${digest.slice(-2)}`;
}

export const BADGE_CODE: Record<StatusId, string> = {
  runtime_ready: "[rt]",
  skeleton_todo: "[td]",
  unsupported: "[un]",
};

export function badgeColor(status: StatusId): string {
  if (status === "skeleton_todo") return "var(--yel)";
  if (status === "unsupported") return "var(--g6)";
  return "var(--ink)";
}

export function badgeWeight(status: StatusId): number {
  return status === "runtime_ready" ? 700 : 400;
}

/** Resolve a JSON pointer against a parsed document. */
export function resolvePointer(root: unknown, pointer: string): unknown {
  if (!pointer.startsWith("/")) return undefined;
  let current: unknown = root;
  for (const rawPart of pointer.slice(1).split("/")) {
    if (current === null || typeof current !== "object") return undefined;
    const part = rawPart.replace(/~1/g, "/").replace(/~0/g, "~");
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

/**
 * Best-effort line number for a JSON pointer inside a YAML document, used to
 * anchor diagnostics in the editor. Walks the key path in order, so it does not
 * need a YAML AST.
 */
export function pointerToLine(yaml: string, pointer: string): number | null {
  if (!pointer.startsWith("/")) return null;
  const parts = pointer
    .slice(1)
    .split("/")
    .filter((part) => part !== "" && !/^\d+$/.test(part));
  if (parts.length === 0) return null;

  const lines = yaml.split("\n");
  let cursor = 0;
  let found: number | null = null;

  for (const part of parts) {
    const pattern = new RegExp(`^\\s*-?\\s*${part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*:`);
    for (let index = cursor; index < lines.length; index += 1) {
      if (pattern.test(lines[index])) {
        found = index + 1;
        cursor = index + 1;
        break;
      }
    }
  }
  return found;
}

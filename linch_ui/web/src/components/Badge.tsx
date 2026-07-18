import type { CapabilityCatalog, StatusId } from "../api/types";
import { BADGE_CODE, badgeColor, badgeWeight } from "../state/derive";

/** The bare `[rt]`/`[td]`/`[un]` code used on canvas nodes and palette rows. */
export function BadgeCode({ status }: { status: StatusId }) {
  return (
    <b style={{ color: badgeColor(status), fontWeight: badgeWeight(status), fontSize: 9 }}>
      {BADGE_CODE[status]}
    </b>
  );
}

/**
 * Code + label, resolved from the catalog so the wording always matches the
 * server ("Runtime-ready" / "Skeleton/TODO" / "Unsupported").
 */
export function BadgeFull({
  status,
  catalog,
}: {
  status: StatusId;
  catalog: CapabilityCatalog | null;
}) {
  const record = catalog?.statuses.find((item) => item.id === status);
  return (
    <span style={{ color: badgeColor(status), fontWeight: badgeWeight(status) }}>
      {BADGE_CODE[status]} {record?.badge ?? status}
    </span>
  );
}

export function statusDescription(status: StatusId, catalog: CapabilityCatalog | null): string {
  return catalog?.statuses.find((item) => item.id === status)?.description ?? "";
}

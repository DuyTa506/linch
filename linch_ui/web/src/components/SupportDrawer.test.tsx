import { describe, expect, it } from "vitest";

import { supportDraftPreview } from "./SupportDrawer";

describe("supportDraftPreview", () => {
  it("renders an incomplete answer string without rendering its JSON envelope", () => {
    expect(supportDraftPreview('{"kind":"answer","answer":"Use \\"run_workflow\\"\\n')).toBe(
      'Use "run_workflow"\n',
    );
  });

  it("uses only the recipe title and overview while the full recipe is incomplete", () => {
    expect(
      supportDraftPreview(
        '{"kind":"recipe","answer":null,"recipe":{"title":"Scheduled review","overview":"Run it from host cron',
      ),
    ).toBe("Scheduled review\nRun it from host cron");
  });
});

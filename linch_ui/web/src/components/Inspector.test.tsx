import { act, render, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it, vi } from "vitest";
import { parse } from "yaml";

import type { StudioApi } from "../api/client";
import type { Blueprint, ProjectDocument } from "../api/types";
import { emitBlueprintYaml } from "../model/emit";
import { MINIMAL_BLUEPRINT } from "../model/fixtures";
import { useStudio, type Studio } from "../state/useStudio";
import { Inspector } from "./Inspector";

function docFor(blueprint: Blueprint): ProjectDocument {
  return {
    id: "demo",
    digest: "a".repeat(64),
    yaml: emitBlueprintYaml(blueprint),
    blueprint,
    diagnostics: [],
    exportReady: true,
    layout: { nodes: [], viewport: { x: 0, y: 0, zoom: 1 } },
  } as ProjectDocument;
}

function Harness({ api, onStudio }: { api: StudioApi; onStudio: (studio: Studio) => void }) {
  const studio = useStudio(api);
  useEffect(() => {
    onStudio(studio);
  });
  return <Inspector studio={studio} width={260} scope={{ kind: "project" }} />;
}

describe("Inspector field commits", () => {
  it("builds a second field's save on the first field's landed edit, not a stale snapshot", async () => {
    const savedYamls: string[] = [];
    let releaseFirstSave: () => void = () => undefined;
    let saveCalls = 0;
    const saveBlueprint = vi.fn((_id: string, yaml: string) => {
      saveCalls += 1;
      savedYamls.push(yaml);
      const document = { ...docFor(parse(yaml) as Blueprint), yaml };
      if (saveCalls === 1) {
        return new Promise<ProjectDocument>((resolve) => {
          releaseFirstSave = () => resolve(document);
        });
      }
      return Promise.resolve(document);
    });
    const api = {
      serviceInfo: vi.fn().mockResolvedValue({ authoringAvailable: false }),
      catalog: vi.fn().mockResolvedValue(null),
      listProjects: vi.fn().mockResolvedValue([]),
      openProject: vi.fn().mockResolvedValue(docFor(MINIMAL_BLUEPRINT)),
      getLayout: vi.fn().mockResolvedValue({ id: "demo", layout: { nodes: [] } }),
      saveBlueprint,
      validateBuffer: vi.fn().mockResolvedValue({ diagnostics: [], structurallyValid: true }),
    } as unknown as StudioApi;

    let studio: Studio | null = null;
    render(<Harness api={api} onStudio={(next) => (studio = next)} />);
    await waitFor(() => expect(studio?.state.ready).toBe(true));
    await act(async () => {
      await studio!.actions.openProject("demo");
    });

    const modelInput = document.getElementById("/spec/runtime/provider/model") as HTMLInputElement;
    const providerSelect = document.getElementById(
      "/spec/runtime/provider/kind",
    ) as HTMLSelectElement;
    expect(modelInput).toBeTruthy();
    expect(providerSelect).toBeTruthy();

    // Field A commits and its save stays pending (network in flight).
    act(() => {
      modelInput.focus();
      modelInput.value = "claude-sonnet-5";
      modelInput.dispatchEvent(new Event("input", { bubbles: true }));
      modelInput.blur();
    });
    await waitFor(() => expect(saveCalls).toBe(1));

    // Field B commits while A is still in flight.
    act(() => {
      providerSelect.value = "anthropic";
      providerSelect.dispatchEvent(new Event("change", { bubbles: true }));
    });

    // Let A land, which should unblock B's queued commit.
    act(() => {
      releaseFirstSave();
    });
    await waitFor(() => expect(saveCalls).toBe(2));

    // B's save must be built on top of A's edit, not a snapshot from before it.
    expect(savedYamls[1]).toContain("claude-sonnet-5");
    expect(savedYamls[1]).toContain("anthropic");
  });
});

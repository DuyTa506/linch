import { act, render, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it, vi } from "vitest";

import type { StudioApi } from "../api/client";
import type { Blueprint, ProjectDocument } from "../api/types";
import { emitBlueprintYaml } from "../model/emit";
import { WORKFLOW_BLUEPRINT } from "../model/fixtures";
import type { FlowNode } from "./StudioNodeView";
import { useStudio, type Studio } from "../state/useStudio";
import { Canvas } from "./Canvas";

const { capturedProps } = vi.hoisted(() => ({
  capturedProps: { current: null as unknown as Record<string, unknown> },
}));

vi.mock("@xyflow/react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@xyflow/react")>();
  return {
    ...actual,
    ReactFlow: (props: Record<string, unknown>) => {
      capturedProps.current = props;
      return null;
    },
    ReactFlowProvider: ({ children }: { children: unknown }) => children,
    useReactFlow: () => ({ zoomIn: vi.fn(), zoomOut: vi.fn(), fitView: vi.fn() }),
    MiniMap: () => null,
    Background: () => null,
  };
});

vi.mock("../model/magnet", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../model/magnet")>();
  return { ...actual, findMagneticConnection: vi.fn(), magneticSnapPosition: vi.fn() };
});

import { findMagneticConnection, magneticSnapPosition } from "../model/magnet";

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
  return <Canvas studio={studio} scope={{ kind: "workflow", id: "pr_review" }} />;
}

function dragNode(id: string, x: number, y: number): FlowNode {
  return { id, position: { x, y } } as unknown as FlowNode;
}

describe("Canvas drag/connect races", () => {
  it("rolls back a rejected magnetic connection using its own drag's origin, unaffected by an intervening drag", async () => {
    const security = "wf_pr_review_security_step";
    const style = "wf_pr_review_style_step";
    const fetchDiff = "wf_pr_review_fetch_diff";

    let rejectSave: (error: unknown) => void = () => undefined;
    const saveBlueprint = vi.fn(
      () =>
        new Promise<ProjectDocument>((_resolve, reject) => {
          rejectSave = reject;
        }),
    );
    const api = {
      serviceInfo: vi.fn().mockResolvedValue({ authoringAvailable: false }),
      catalog: vi.fn().mockResolvedValue(null),
      listProjects: vi.fn().mockResolvedValue([]),
      openProject: vi.fn().mockResolvedValue(docFor(WORKFLOW_BLUEPRINT)),
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
    await waitFor(() => expect(capturedProps.current).not.toBeNull());

    const onNodeDragStart = capturedProps.current.onNodeDragStart as (
      event: unknown,
      node: FlowNode,
    ) => void;
    const onNodeDragStop = capturedProps.current.onNodeDragStop as (
      event: unknown,
      node: FlowNode,
    ) => void;

    // Drag A: security -> style snaps magnetically; the connect is kicked off
    // but stays pending (saveBlueprint has not resolved yet).
    vi.mocked(findMagneticConnection).mockReturnValueOnce({ source: security, target: style, distance: 10 });
    vi.mocked(magneticSnapPosition).mockReturnValueOnce({ x: 200, y: 200 });
    act(() => {
      onNodeDragStart(null, dragNode(security, 100, 100));
    });
    act(() => {
      onNodeDragStop(null, dragNode(security, 150, 150));
    });

    // Drag B: an unrelated node, dropped far from anything else, starts and
    // finishes entirely *while drag A's connect is still in flight*.
    vi.mocked(findMagneticConnection).mockReturnValueOnce(null);
    act(() => {
      onNodeDragStart(null, dragNode(fetchDiff, 5, 5));
    });
    act(() => {
      onNodeDragStop(null, dragNode(fetchDiff, 5000, 5000));
    });

    // Drag A's connect now resolves as rejected by the server.
    act(() => {
      rejectSave(new Error("stale digest"));
    });

    await waitFor(() => {
      const nodes = capturedProps.current.nodes as FlowNode[];
      const securityNode = nodes.find((node) => node.id === security);
      expect(securityNode?.position).toEqual({ x: 100, y: 100 });
    });
  });
});

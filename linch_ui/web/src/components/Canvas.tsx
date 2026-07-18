import {
  Background,
  BackgroundVariant,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type NodeChange,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { LayoutDocument } from "../api/types";
import { useT } from "../i18n";
import {
  AUTO_LAYOUT_COLUMN,
  blueprintToGraph,
  catalogAllowsConnection,
  connectSemanticRelation,
  fallbackNodePosition,
  graphForScope,
  type CanvasScope,
} from "../model/graph";
import {
  findMagneticConnection,
  findNearbyNode,
  isRuntimeSubagentPair,
  MAGNET_RADIUS,
  magneticSnapPosition,
  type MagneticConnection,
} from "../model/magnet";
import { addPaletteItem, buildPalette } from "../model/palette";
import type { Studio } from "../state/useStudio";
import { StudioNodeView, type FlowNode } from "./StudioNodeView";

import "@xyflow/react/dist/style.css";

const NODE_TYPES = { studio: StudioNodeView };

const RELATION_LABEL = {
  depends_on: "A2A FLOW",
  subagent_binding: "SUBAGENT",
  tool_filter: "TOOL ACCESS",
  allowed_tool: "ALLOWED TOOL",
  trigger_binding: "TRIGGER",
  routine_target: "TARGET",
} as const;

function Inner({ studio, scope }: { studio: Studio; scope: CanvasScope }) {
  const t = useT();
  const { state, blueprint, actions } = studio;
  const flow = useReactFlow();
  const [zoom, setZoom] = useState(1);
  const [connecting, setConnecting] = useState(false);
  const [invalidDrop, setInvalidDrop] = useState(false);
  const [magnetic, setMagnetic] = useState<MagneticConnection | null>(null);
  const connectionCommitted = useRef(false);
  const nodesRef = useRef<FlowNode[]>([]);
  const dragOrigin = useRef<{ id: string; position: { x: number; y: number } } | null>(null);

  const graph = useMemo(
    () => (blueprint ? graphForScope(blueprintToGraph(blueprint), scope) : null),
    [blueprint, scope],
  );

  const [nodes, setNodes] = useState<FlowNode[]>([]);

  // Rebuild cards from the Blueprint, but take positions from the layout file so
  // a semantic edit never moves the canvas.
  const selectedIdRef = useRef(state.selectedId);
  selectedIdRef.current = state.selectedId;
  useEffect(() => {
    if (!graph) return;
    const saved = new Map((state.layout.nodes ?? []).map((node) => [node.id, node]));
    const perColumn = new Map<number, number>();
    const next = graph.nodes.map((node) => {
        const column = AUTO_LAYOUT_COLUMN[node.data.kind] ?? 2;
        const index = perColumn.get(column) ?? 0;
        perColumn.set(column, index + 1);
        const stored = saved.get(node.id);
        return {
          id: node.id,
          type: "studio" as const,
          position: stored
            ? { x: stored.x, y: stored.y }
            : fallbackNodePosition(graph, node, index, saved),
          data: node.data,
          selected: node.id === selectedIdRef.current,
        };
      });
    nodesRef.current = next;
    setNodes(next);
  }, [graph, state.layout]);

  // Selection re-flags cards in place. Rebuilding them here instead would hand
  // React Flow fresh objects with no `measured` size, and a card it has already
  // mounted is never re-measured — it would just stay invisible.
  useEffect(() => {
    setNodes((current) => {
      let changed = false;
      const next = current.map((node) => {
        const selected = node.id === state.selectedId;
        if (node.selected === selected) return node;
        changed = true;
        return { ...node, selected };
      });
      if (!changed) return current;
      nodesRef.current = next;
      return next;
    });
  }, [state.selectedId]);

  const edges = useMemo<Edge[]>(() => {
    const semanticEdges: Edge[] = (graph?.edges ?? []).map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        sourceHandle: edge.variant === "attach" ? "attachment-out" : "workflow-out",
        targetHandle: edge.variant === "attach" ? "attachment-in" : "workflow-in",
        type: "smoothstep" as const,
        animated: edge.variant === "flow",
        label: RELATION_LABEL[edge.relation],
        labelStyle: { fontSize: 8, fontWeight: 700, letterSpacing: "0.04em" },
        labelBgPadding: [4, 3] as [number, number],
        labelBgBorderRadius: 0,
        labelBgStyle: { fill: "var(--card)", stroke: "var(--dot)" },
        className: `${
          edge.variant === "attach" ? "react-flow__edge--attach" : "react-flow__edge--flow"
        }${edge.id === state.recentConnection?.edgeId ? " react-flow__edge--attachment-saved" : ""}`,
        selected: edge.id === state.selectedEdgeId,
      }));
    if (magnetic) {
      semanticEdges.push({
        id: "magnetic-preview",
        source: magnetic.source,
        target: magnetic.target,
        sourceHandle: "attachment-out",
        targetHandle: "attachment-in",
        type: "smoothstep",
        animated: true,
        selectable: false,
        className: "react-flow__edge--attach react-flow__edge--preview",
      });
    }
    return semanticEdges;
  }, [graph, magnetic, state.recentConnection?.edgeId, state.selectedEdgeId]);

  const renderedNodes = useMemo(
    () => {
      const canAttach = (source: FlowNode, target: FlowNode) => {
        if (!blueprint || source.id === target.id) return false;
        if (source.data.kind === "workflow_step" && target.data.kind === "workflow_step") {
          return false;
        }
        return (
          catalogAllowsConnection(state.catalog, blueprint, source.id, target.id) &&
          connectSemanticRelation(blueprint, source.id, target.id).connected
        );
      };
      return nodes.map((node) => {
        const classes = [];
        if (node.id === magnetic?.target) classes.push("react-flow__node--magnetic-target");
        if (node.id === magnetic?.source) classes.push("react-flow__node--magnetic-source");
        if (
          node.id === state.recentConnection?.sourceId ||
          node.id === state.recentConnection?.targetId
        ) {
          classes.push("react-flow__node--attachment-saved");
        }
        return {
          ...node,
          data: {
            ...node.data,
            canAttachOut: nodes.some((candidate) => canAttach(node, candidate)),
            canAttachIn: nodes.some((candidate) => canAttach(candidate, node)),
          },
          className: classes.join(" ") || undefined,
        };
      });
    },
    [blueprint, magnetic, nodes, state.catalog, state.recentConnection],
  );

  const isValidConnection = useCallback(
    (connection: Connection | Edge) => {
      if (!blueprint || !connection.source || !connection.target) return false;
      if (!catalogAllowsConnection(state.catalog, blueprint, connection.source, connection.target)) {
        return false;
      }
      return connectSemanticRelation(blueprint, connection.source, connection.target).connected;
    },
    [blueprint, state.catalog],
  );

  const relationAllowed = useCallback(
    (sourceId: string, targetId: string) =>
      Boolean(blueprint && catalogAllowsConnection(state.catalog, blueprint, sourceId, targetId)),
    [blueprint, state.catalog],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target || !isValidConnection(connection)) return;
      connectionCommitted.current = true;
      void actions.connectNodes(connection.source, connection.target);
    },
    [actions, isValidConnection],
  );

  const persist = useCallback(
    (next: FlowNode[]) => {
      const positions = new Map(
        (state.layout.nodes ?? []).map((node) => [node.id, { ...node }]),
      );
      for (const node of next) {
        positions.set(node.id, {
          id: node.id,
          x: Math.round(node.position.x),
          y: Math.round(node.position.y),
        });
      }
      const layout: LayoutDocument = {
        nodes: [...positions.values()],
        viewport: state.layout.viewport,
      };
      actions.saveLayout(layout);
    },
    [actions, state.layout.nodes, state.layout.viewport],
  );

  const onNodesChange = useCallback(
    (changes: NodeChange<FlowNode>[]) => {
      setNodes((current) => {
        const next = applyNodeChanges(changes, current);
        nodesRef.current = next;
        if (changes.some((change) => change.type === "position" && change.dragging === false)) {
          persist(next);
        }
        return next;
      });
    },
    [persist],
  );

  const nodesWithDraggedPosition = useCallback((dragged: FlowNode) => {
    return nodesRef.current.map((node) =>
      node.id === dragged.id ? { ...node, position: dragged.position } : node,
    );
  }, []);

  const rejectNearbyDrop = useCallback(
    (dragged: FlowNode, nearby: FlowNode) => {
      setInvalidDrop(true);
      window.setTimeout(() => setInvalidDrop(false), 260);
      if (blueprint && isRuntimeSubagentPair(blueprint, dragged.id, nearby.id)) {
        actions.toast("[!!]", t.subagentRuntimeHint);
      } else {
        actions.toast("[xx]", t.incompatibleConnection);
      }
    },
    [actions, blueprint, t.incompatibleConnection, t.subagentRuntimeHint],
  );

  const onNodeDrag = useCallback(
    (_event: MouseEvent | TouchEvent, dragged: FlowNode) => {
      if (!blueprint) return;
      const positioned = nodesWithDraggedPosition(dragged);
      setMagnetic(
        findMagneticConnection(blueprint, dragged, positioned, MAGNET_RADIUS, relationAllowed),
      );
    },
    [blueprint, nodesWithDraggedPosition, relationAllowed],
  );

  const onNodeDragStop = useCallback(
    (_event: MouseEvent | TouchEvent, dragged: FlowNode) => {
      if (!blueprint) return;
      const positioned = nodesWithDraggedPosition(dragged);
      const connection = findMagneticConnection(
        blueprint,
        dragged,
        positioned,
        MAGNET_RADIUS,
        relationAllowed,
      );
      setMagnetic(null);
      if (connection) {
        const snap = magneticSnapPosition(positioned, dragged.id, connection);
        const snapped = positioned.map((node) =>
          node.id === dragged.id && snap ? { ...node, position: snap } : node,
        );
        nodesRef.current = snapped;
        setNodes(snapped);
        persist(snapped);
        void actions.connectNodes(connection.source, connection.target).then((saved) => {
          if (!saved) {
            const origin = dragOrigin.current;
            if (origin?.id === dragged.id) {
              const rolledBack = nodesRef.current.map((node) =>
                node.id === dragged.id ? { ...node, position: origin.position } : node,
              );
              nodesRef.current = rolledBack;
              setNodes(rolledBack);
              persist(rolledBack);
            }
            setInvalidDrop(true);
            window.setTimeout(() => setInvalidDrop(false), 260);
          }
          dragOrigin.current = null;
        });
        return;
      }

      const nearby = findNearbyNode(dragged, positioned) as FlowNode | null;
      const alreadyConnected = nearby
        ? (graph?.edges ?? []).some(
            (edge) =>
              (edge.source === dragged.id && edge.target === nearby.id) ||
              (edge.source === nearby.id && edge.target === dragged.id),
          )
        : false;
      if (nearby && !alreadyConnected) rejectNearbyDrop(dragged, nearby);
      dragOrigin.current = null;
    },
    [
      actions,
      blueprint,
      graph?.edges,
      nodesWithDraggedPosition,
      persist,
      rejectNearbyDrop,
      relationAllowed,
    ],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      const selected = changes.find(
        (change): change is Extract<EdgeChange, { type: "select" }> =>
          change.type === "select" && change.selected,
      );
      if (selected) actions.selectEdge(selected.id);
    },
    [actions],
  );

  const onDrop = useCallback(
    async (event: React.DragEvent) => {
      event.preventDefault();
      const itemId = event.dataTransfer.getData("application/linch-palette");
      if (!itemId || !blueprint) return;
      const item = buildPalette(state.catalog).find((entry) => entry.id === itemId);
      if (!item?.creatable) return;
      const result = addPaletteItem(
        blueprint,
        item,
        scope.kind === "workflow" ? { workflowId: scope.id } : {},
      );
      if (!result.added) return;
      await actions.applyBlueprint(result.blueprint);
    },
    [blueprint, state.catalog, actions, scope],
  );

  if (graph && graph.nodes.length === 0) {
    return (
      <div className="canvas">
        <div className="empty">
          <div className="empty__glyph">+</div>
          <b>{t.emptyTitle}</b>
          <div className="empty__body">{t.emptyBody}</div>
        </div>
      </div>
    );
  }

  return (
    <div
      className={`canvas${connecting || magnetic ? " canvas--connecting" : ""}${
        invalidDrop ? " canvas--invalid" : ""
      }`}
      onDrop={(event) => void onDrop(event)}
      onDragOver={(event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }}
      data-testid="canvas"
    >
      <ReactFlow
        nodes={renderedNodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(event, node) => {
          event.stopPropagation();
          actions.select(node.id);
        }}
        onEdgeClick={(event, edge) => {
          event.stopPropagation();
          actions.selectEdge(edge.id);
        }}
        // Selection is owned by the click handlers below. React Flow reports its
        // own selection asynchronously, so echoing it back here would clobber a
        // selection made in code — such as handing the inspector to a new card.
        onPaneClick={() => actions.select(null)}
        onNodeDragStart={(_event, node) => {
          dragOrigin.current = { id: node.id, position: { ...node.position } };
        }}
        onNodeDrag={onNodeDrag}
        onNodeDragStop={onNodeDragStop}
        onMove={(_, viewport) => setZoom(viewport.zoom)}
        onConnect={onConnect}
        onConnectStart={() => {
          connectionCommitted.current = false;
          setConnecting(true);
        }}
        onConnectEnd={() => {
          setConnecting(false);
          if (!connectionCommitted.current) {
            setInvalidDrop(true);
            window.setTimeout(() => setInvalidDrop(false), 260);
          }
        }}
        isValidConnection={isValidConnection}
        connectionRadius={36}
        connectionLineStyle={{ stroke: "var(--ink)", strokeWidth: 2, strokeDasharray: "5 4" }}
        minZoom={0.5}
        maxZoom={1.4}
        proOptions={{ hideAttribution: true }}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
        nodesConnectable
        deleteKeyCode={null}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="var(--dot2)" />
        <MiniMap
          nodeColor="var(--ink)"
          maskColor="rgba(0,0,0,0.06)"
          style={{ width: 132, height: 84, pointerEvents: "none" }}
        />
      </ReactFlow>

      <div className={`canvas__hint${magnetic ? " canvas__hint--magnetic" : ""}`}>
        {magnetic ? t.magneticReady : t.canvasHint}
      </div>

      {state.recentConnection && (
        <div className="canvas__connection-confirmation" role="status">
          <b>[ok] {RELATION_LABEL[state.recentConnection.relation as keyof typeof RELATION_LABEL]}</b>
          <span>
            {state.recentConnection.sourceTitle} → {state.recentConnection.targetTitle}
          </span>
          <small>{t.connectionPersisted}</small>
        </div>
      )}

      <div className="zoombar">
        <button className="zoombar__btn" onClick={() => void flow.zoomIn()} aria-label="zoom in">
          +
        </button>
        <button className="zoombar__btn" onClick={() => void flow.zoomOut()} aria-label="zoom out">
          −
        </button>
        <button
          className="zoombar__btn"
          style={{ fontSize: 10, padding: "5px 10px" }}
          onClick={() => void flow.fitView({ padding: 0.2, maxZoom: 1 })}
        >
          {t.fit}
        </button>
        <span className="zoombar__pct">{Math.round(zoom * 100)}%</span>
      </div>
    </div>
  );
}

export function Canvas({ studio, scope }: { studio: Studio; scope: CanvasScope }) {
  return (
    <ReactFlowProvider>
      <Inner studio={studio} scope={scope} />
    </ReactFlowProvider>
  );
}

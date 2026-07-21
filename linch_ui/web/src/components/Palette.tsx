import { useMemo, useState } from "react";

import { useT } from "../i18n";
import { type CanvasScope, blueprintToGraph, graphForScope, toLayoutId } from "../model/graph";
import {
  addPaletteItem,
  buildPalette,
  declaredHint,
  groupPalette,
  unsupportedItems,
} from "../model/palette";
import type { Studio } from "../state/useStudio";
import { BadgeCode } from "./Badge";

export function Palette({
  studio,
  width,
  scope,
  workflowId,
  onOpenRoutine,
}: {
  studio: Studio;
  width: number;
  scope: CanvasScope;
  workflowId?: string;
  onOpenRoutine?: (routineId: string) => void;
}) {
  const t = useT();
  const { state, blueprint, actions } = studio;
  const [open, setOpen] = useState(true);
  const [query, setQuery] = useState("");

  const groups = useMemo(() => {
    const workflowOnly = new Set(["agent_call", "workflow_run_routine", "scheduled_workflow"]);
    const items = buildPalette(state.catalog).filter(
      (item) =>
        (workflowId || !workflowOnly.has(item.id)) &&
        item.name.toLowerCase().includes(query.trim().toLowerCase()),
    );
    return groupPalette(items);
  }, [state.catalog, query, workflowId]);

  const unsupported = useMemo(() => unsupportedItems(state.catalog), [state.catalog]);

  if (!open) {
    return (
      <button className="rail hoverable" title={t.expand} onClick={() => setOpen(true)}>
        »
      </button>
    );
  }

  const add = async (itemId: string) => {
    if (!blueprint) return;
    const item = buildPalette(state.catalog).find((entry) => entry.id === itemId);
    if (!item?.creatable) return;
    const result = addPaletteItem(blueprint, item, workflowId ? { workflowId } : {});
    if (result.added) {
      const saved = await actions.applyBlueprint(result.blueprint);
      if (!saved) return;
      if (["agent_tick_routine", "workflow_run_routine", "scheduled_workflow"].includes(item.id)) {
        onOpenRoutine?.(result.added);
      }
      // Declaring a card and attaching it are separate steps; say which one just
      // happened rather than letting the card land silently.
      const graph = blueprintToGraph(result.blueprint);
      const added = graph.nodes.find((node) => node.data.sourceId === result.added);
      // Only a Tool is selected on arrival, to put its Attach control in reach.
      // Selecting any newly created card re-renders the canvas before React Flow
      // has measured it, which leaves the card stuck invisible.
      const selectable =
        added &&
        added.data.kind === "tool" &&
        graphForScope(graph, scope).nodes.some((item) => item.id === added.id);
      if (selectable) actions.select(added.id);
      const hint = declaredHint(item.id, {
        inWorkflow: Boolean(workflowId),
        attached: graph.edges.some(
          (edge) =>
            edge.source === added?.id &&
            edge.target === toLayoutId("primary_agent") &&
            edge.relation === "tool_filter",
        ),
      });
      if (hint) actions.toast(hint.marker, t[hint.key as keyof typeof t] as string);
    } else if (result.reason === "workflow_required") {
      actions.toast("[!!]", "Open a directed workflow scope before adding this item.");
    }
  };

  return (
    <div className="palette" style={{ width }}>
      <div className="palette__head">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t.search}
          aria-label={t.search}
          style={{ flex: 1, fontSize: 10, padding: "6px 9px" }}
        />
        <button
          className="btn-outline"
          style={{ padding: "4px 7px", fontSize: 10, fontWeight: 700 }}
          title={t.collapse}
          onClick={() => setOpen(false)}
        >
          «
        </button>
      </div>

      <div className="palette__body">
        {groups.map(([category, items]) => (
          <div key={category}>
            <div className="palette__cat">{category}</div>
            {items.map((item) => (
              <button
                key={item.id}
                className="palette__item hoverable"
                title={item.tip}
                draggable={item.creatable}
                disabled={!item.creatable}
                onDragStart={(event) => {
                  event.dataTransfer.setData("application/linch-palette", item.id);
                  event.dataTransfer.effectAllowed = "copy";
                }}
                onClick={() => void add(item.id)}
              >
                <span>
                  {item.glyph} {item.name}
                </span>
                <BadgeCode status={item.status} />
              </button>
            ))}
          </div>
        ))}

        {unsupported.length > 0 && (
          <div>
            <div className="palette__cat">UNSUPPORTED</div>
            {unsupported.map((record) => (
              <div
                key={record.id}
                className="palette__item"
                title={record.summary}
                style={{ cursor: "help", opacity: 0.8 }}
              >
                <span style={{ color: "var(--g6)" }}>{record.title}</span>
                <BadgeCode status={record.status} />
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="palette__foot">
        {t.paletteLegend1}
        <br />
        {t.paletteLegend2}
      </div>
    </div>
  );
}

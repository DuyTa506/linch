import { useCallback, useEffect, useMemo, useState } from "react";

import type { StudioApi } from "../api/client";
import { useT } from "../i18n";
import { blueprintToGraph, type CanvasScope } from "../model/graph";
import { countBy, hasErrors, shortDigest } from "../state/derive";
import type { Studio as StudioController, Tab } from "../state/useStudio";
import { Canvas } from "./Canvas";
import { DiagnosticsTab } from "./DiagnosticsTab";
import { ExportTab } from "./ExportTab";
import { FilesTab } from "./FilesTab";
import { Inspector } from "./Inspector";
import { Palette } from "./Palette";
import { YamlTab } from "./YamlTab";

const PALETTE = { min: 170, max: 360, initial: 238 };
const INSPECTOR = { min: 220, max: 420, initial: 296 };

function scopeKey(scope: CanvasScope): string {
  return scope.kind === "project" || scope.kind === "agent"
    ? scope.kind
    : `${scope.kind}:${scope.id}`;
}

function useResizer(initial: number, min: number, max: number, invert = false) {
  const [width, setWidth] = useState(initial);
  const start = useCallback(
    (event: React.MouseEvent) => {
      event.preventDefault();
      const originX = event.clientX;
      const originWidth = width;
      const move = (moveEvent: MouseEvent) => {
        const delta = (moveEvent.clientX - originX) * (invert ? -1 : 1);
        setWidth(Math.min(max, Math.max(min, originWidth + delta)));
      };
      const stop = () => {
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", stop);
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", stop);
    },
    [width, min, max, invert],
  );
  return { width, start };
}

/** The save chip: one honest read of what is and isn't on disk. */
function SaveChip({ studio }: { studio: StudioController }) {
  const t = useT();
  const { state } = studio;
  const map = {
    saved: { mark: "[ok]", label: t.saved, tip: t.savedTip, color: "var(--ink)" },
    draft: { mark: "[!!]", label: t.draftSaved, tip: t.draftSavedTip, color: "var(--yel)" },
    buffer: { mark: "[xx]", label: t.unsavedBuffer, tip: t.unsavedBufferTip, color: "var(--red)" },
    conflict: { mark: "[xx]", label: t.saveConflict, tip: t.saveConflictTip, color: "var(--red)" },
    new: { mark: "[--]", label: t.newUnsaved, tip: t.newUnsavedTip, color: "var(--ink)" },
    saving: { mark: "[--]", label: t.saving, tip: t.savingTip, color: "var(--g6)" },
  } as const;
  const chip = map[state.saveState];
  return (
    <span className="chip" title={chip.tip}>
      <b style={{ color: chip.color }}>{chip.mark}</b> {chip.label}
    </span>
  );
}

export function Studio({
  studio,
  api,
  onOpenAi,
  onOpenDocumentation,
  onOpenSettings,
  lang,
}: {
  studio: StudioController;
  api: StudioApi;
  onOpenAi: () => void;
  onOpenDocumentation: () => void;
  onOpenSettings: () => void;
  lang: string;
}) {
  const t = useT();
  const { state, blueprint, actions } = studio;
  const palette = useResizer(PALETTE.initial, PALETTE.min, PALETTE.max);
  const inspector = useResizer(INSPECTOR.initial, INSPECTOR.min, INSPECTOR.max, true);
  const [scope, setScope] = useState<CanvasScope>({ kind: "project" });

  const graph = useMemo(() => (blueprint ? blueprintToGraph(blueprint) : null), [blueprint]);
  const errors = countBy(state.diagnostics, "error");
  const warnings = countBy(state.diagnostics, "warning");
  const blocked = hasErrors(state.diagnostics) || !state.structurallyValid;
  const scopes = useMemo<Array<{ scope: CanvasScope; label: string }>>(
    () => [
      { scope: { kind: "project" }, label: "Project map" },
      { scope: { kind: "agent" }, label: "Agent loop" },
      ...(blueprint?.spec.workflows ?? []).map((workflow) => ({
        scope: { kind: "workflow" as const, id: workflow.id },
        label: `Workflow · ${workflow.displayName || workflow.id}`,
      })),
      ...(blueprint?.spec.routines ?? []).map((routine) => ({
        scope: { kind: "routine" as const, id: routine.id },
        label: `Routine · ${routine.displayName || routine.id}`,
      })),
    ],
    [blueprint],
  );

  useEffect(() => {
    if (!scopes.some((item) => scopeKey(item.scope) === scopeKey(scope))) {
      setScope({ kind: "project" });
    }
  }, [scope, scopes]);

  // Del removes the selected node, but never while typing in a field.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        if (state.tab !== "design" || state.editHistory.length === 0) return;
        event.preventDefault();
        void actions.undoLastEdit();
        return;
      }
      if (event.key !== "Delete" && event.key !== "Backspace") return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || target?.isContentEditable) return;
      if (state.tab !== "design" || (!state.selectedId && !state.selectedEdgeId)) return;
      event.preventDefault();
      if (state.selectedEdgeId) {
        void actions.disconnectEdge(state.selectedEdgeId);
      } else {
        void actions.removeSelected();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [state.tab, state.selectedId, state.selectedEdgeId, state.editHistory.length, actions]);

  const tabs: { id: Tab; label: string }[] = [
    { id: "design", label: t.tabDesign },
    { id: "yaml", label: t.tabYaml },
    {
      id: "diagnostics",
      label: `${t.tabDiagnostics} [${state.diagnostics.length}]`,
    },
    { id: "files", label: t.tabFiles },
    { id: "export", label: t.tabExport },
  ];

  const statusLeft = graph
    ? [
        `${graph.nodes.length} ${t.nodes}`,
        `${graph.edges.length} ${t.edges}`,
        ...(graph.outputs.length > 0 ? [`${t.output}: ${graph.outputs.length}`] : []),
        t.designOnly,
      ].join(" · ")
    : t.designOnly;

  return (
    <>
      <div className="topbar">
        <button className="topbar__brand" onClick={actions.closeProject}>
          {t.brand}
        </button>
        <span style={{ color: "var(--g9)" }}>::</span>
        <b>{state.doc?.blueprint.metadata.title}</b>
        <span style={{ color: "var(--g9)" }}>[{state.doc?.id}]</span>
        <SaveChip studio={studio} />
        <span style={{ color: "var(--g9)", fontSize: 10 }} title={t.digestTip}>
          digest {state.doc ? shortDigest(state.doc.digest) : "—"}
          {state.conflictDigest && (
            <b style={{ color: "var(--red)" }}> ≠ {shortDigest(state.conflictDigest)}</b>
          )}
        </span>

        <span style={{ flex: 1 }} />

        <span
          style={{ color: "var(--g6)", fontSize: 10 }}
          title={state.authoringAvailable ? t.aiTipReady : t.aiTipOff}
        >
          [ai: {state.authoringAvailable ? t.aiReady : t.aiOff}]
        </span>
        <button className="link" onClick={onOpenDocumentation}>
          ? {t.docs.navLabel.toLowerCase()}
        </button>
        <button
          className="btn-outline hoverable"
          style={{ padding: "3px 9px", fontSize: 11 }}
          title={t.settings}
          onClick={onOpenSettings}
        >
          ⚙ {lang.toUpperCase()}
        </button>
        <button className="link" onClick={onOpenAi}>
          ✻ Support
        </button>
        <button className="btn-outline" onClick={() => actions.setTab("diagnostics")}>
          {t.validate}
        </button>
        <button className="btn-solid" onClick={() => actions.setTab("export")}>
          {t.export} →
        </button>
      </div>

      {state.saveState === "conflict" && (
        <div
          style={{
            flex: "none",
            display: "flex",
            gap: 12,
            alignItems: "center",
            justifyContent: "space-between",
            padding: "8px 18px",
            borderBottom: "1px solid var(--ink)",
            background: "var(--mut)",
            fontSize: 10.5,
          }}
          role="alert"
        >
          <span>
            <b style={{ color: "var(--red)" }}>[xx] {t.saveConflict}</b> — {t.saveConflictTip}
          </span>
          <button className="btn-outline" onClick={() => void actions.reloadFromDisk()}>
            {t.revert}
          </button>
        </div>
      )}

      {state.doc?.migratedFrom && (
        <div className="migration-banner" role="status">
          <span>
            <b>[!!] Migrated in memory from {state.doc.migratedFrom}.</b>{" "}
            The original YAML has not been overwritten.
            {(state.doc.migrationWarnings ?? []).map((warning) => (
              <span key={`${warning.code}:${warning.path}`}> {warning.message}</span>
            ))}
          </span>
          <button className="btn-solid" onClick={() => void actions.applyBlueprint(blueprint!)}>
            Save migrated blueprint
          </button>
        </div>
      )}

      <div className="tabs">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            className={`tab${state.tab === tab.id ? " tab--active" : ""}`}
            onClick={() => actions.setTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
        <span style={{ flex: 1 }} />
        <span
          style={{
            padding: "8px 18px",
            fontSize: 10,
            color: blocked ? "var(--red)" : "var(--g6)",
          }}
        >
          {errors} {t.errWord} · {warnings} {t.warnWord} ·{" "}
          {blocked ? t.exportBlocked : t.exportOk}
        </span>
      </div>

      <div className="body">
        {state.tab === "design" && (
          <>
            <Palette
              studio={studio}
              width={palette.width}
              scope={scope}
              workflowId={scope.kind === "workflow" ? scope.id : undefined}
              onOpenRoutine={(id) => setScope({ kind: "routine", id })}
            />
            <div className="resizer hoverable" onMouseDown={palette.start} />
            <div className="canvas-stack">
              <div className="scopebar" aria-label="Canvas scope">
                {scopes.map((item) => (
                  <button
                    key={scopeKey(item.scope)}
                    className={`scopebar__item${
                      scopeKey(item.scope) === scopeKey(scope) ? " scopebar__item--active" : ""
                    }`}
                    onClick={() => {
                      actions.select(null);
                      setScope(item.scope);
                    }}
                  >
                    {item.label}
                  </button>
                ))}
                <span style={{ flex: 1 }} />
                {state.selectedEdgeId && (
                  <button
                    className="scopebar__item"
                    onClick={() => void actions.disconnectEdge(state.selectedEdgeId!)}
                  >
                    Delete edge
                  </button>
                )}
                <button
                  className="scopebar__item"
                  disabled={state.editHistory.length === 0}
                  onClick={() => void actions.undoLastEdit()}
                >
                  Undo
                </button>
              </div>
              <Canvas studio={studio} scope={scope} />
            </div>
            <div className="resizer hoverable" onMouseDown={inspector.start} />
            <Inspector studio={studio} width={inspector.width} scope={scope} />
          </>
        )}
        {state.tab === "yaml" && <YamlTab studio={studio} />}
        {state.tab === "diagnostics" && <DiagnosticsTab studio={studio} />}
        {state.tab === "files" && <FilesTab studio={studio} api={api} />}
        {state.tab === "export" && <ExportTab studio={studio} api={api} />}
      </div>

      <div className="statusbar">
        <span>{statusLeft}</span>
        <span>
          {t.validateWord}:{" "}
          <b style={{ color: blocked ? "var(--red)" : "var(--ink)" }}>
            {blocked ? "[xx]" : "[ok]"}
          </b>{" "}
          {errors} {t.errWord} · {warnings} {t.warnWord}
        </span>
      </div>
    </>
  );
}

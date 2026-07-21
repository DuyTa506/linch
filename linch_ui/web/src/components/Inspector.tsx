import { Fragment, useMemo, useRef, useState } from "react";

import { useT } from "../i18n";
import { FIELDS, PROJECT_FIELDS, type FieldSpec } from "../model/fields";
import { type CanvasScope, blueprintToGraph, toLayoutId } from "../model/graph";
import { setAtPointer } from "../model/palette";
import { resolvePointer } from "../state/derive";
import type { Studio } from "../state/useStudio";
import { BadgeFull, statusDescription } from "./Badge";

function toValue(spec: FieldSpec, raw: string): unknown {
  if (raw.trim() === "") return undefined;
  if (spec.type === "number") {
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  if (spec.type === "json") return JSON.parse(raw) as unknown;
  if (spec.type === "select" && (raw === "true" || raw === "false")) return raw === "true";
  return raw;
}

/**
 * Render a list of strict-model fields rooted at one JSON pointer.
 *
 * Shared by the node inspector and the project configuration rail so both write
 * back through exactly one path.
 *
 * Args:
 *     studio: Studio store, used to read the Blueprint and save edits
 *     base: JSON pointer the specs' keys are relative to
 *     specs: Fields to render, in display order and grouped by section
 *     onSaved: Called after a field is written to disk
 */
function FieldList({
  studio,
  base,
  specs,
  onSaved,
}: {
  studio: Studio;
  base: string;
  specs: FieldSpec[];
  onSaved?: () => void;
}) {
  const t = useT();
  const { blueprint, actions } = studio;
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  // Always read the latest saved Blueprint, not the one closed over when a
  // commit was queued — a second field's edit must build on the first's.
  const blueprintRef = useRef(blueprint);
  blueprintRef.current = blueprint;
  // Serializes commits: a second field committed while the first is still
  // saving waits for it, so it never overwrites that landed edit.
  const commitQueue = useRef(Promise.resolve());

  if (!blueprint) return null;

  const commit = (spec: FieldSpec, raw: string) => {
    const pointer = `${base}/${spec.key}`;
    setDraft((current) => ({ ...current, [pointer]: raw }));
    commitQueue.current = commitQueue.current.then(async () => {
      const current = blueprintRef.current;
      if (!current) return;
      try {
        const next = setAtPointer(current, pointer, toValue(spec, raw));
        setFieldErrors((prev) => ({ ...prev, [pointer]: "" }));
        const saved = await actions.applyBlueprint(next);
        if (saved) onSaved?.();
      } catch {
        setFieldErrors((prev) => ({ ...prev, [pointer]: "Enter valid JSON." }));
      }
    });
    return commitQueue.current;
  };

  return (
    <>
      {specs.map((spec, index) => {
        const pointer = `${base}/${spec.key}`;
        const stored = resolvePointer(blueprint, pointer);
        const serialized =
          stored === undefined
            ? ""
            : spec.type === "json"
              ? JSON.stringify(stored, null, 2)
              : String(stored);
        const value = draft[pointer] ?? serialized;
        const previous = specs[index - 1];
        return (
          <Fragment key={spec.key}>
            {previous?.section !== spec.section && (
              <div className="inspector__section">{spec.section}</div>
            )}
            <div className="field">
              <label className="field__label" htmlFor={pointer}>
                <span>{spec.label}</span>
                {spec.env && (
                  <span className="field__env" title={t.envTip}>
                    env
                  </span>
                )}
                {spec.required && <span> *</span>}
              </label>
              {spec.type === "select" ? (
                <select
                  id={pointer}
                  className="field__value"
                  value={value}
                  onChange={(event) => void commit(spec, event.target.value)}
                >
                  <option value="">—</option>
                  {spec.options?.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              ) : spec.type === "json" ? (
                <textarea
                  id={pointer}
                  className="field__value"
                  rows={7}
                  value={value}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, [pointer]: event.target.value }))
                  }
                  onBlur={(event) => void commit(spec, event.target.value)}
                />
              ) : (
                <input
                  id={pointer}
                  className="field__value"
                  value={value}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, [pointer]: event.target.value }))
                  }
                  onBlur={(event) => void commit(spec, event.target.value)}
                />
              )}
              {fieldErrors[pointer] && (
                <div className="field__desc" style={{ color: "var(--red)" }}>
                  {fieldErrors[pointer]}
                </div>
              )}
              {spec.desc && <div className="field__desc">{spec.desc}</div>}
            </div>
          </Fragment>
        );
      })}
    </>
  );
}

/** Provider, memory, and MCP wiring — configuration the canvas deliberately never draws. */
function ProjectRail({
  studio,
  width,
  onCollapse,
}: {
  studio: Studio;
  width: number;
  onCollapse: () => void;
}) {
  const t = useT();
  const [saved, setSaved] = useState(false);

  return (
    <div className="inspector" style={{ width }} data-testid="project-rail">
      <div className="inspector__head">
        <div style={{ fontSize: 12, display: "flex", justifyContent: "space-between" }}>
          <b>{t.railTitle}</b>
          <button
            className="btn-outline"
            style={{ padding: "2px 6px", fontSize: 10, fontWeight: 700 }}
            onClick={onCollapse}
          >
            »
          </button>
        </div>
        <div style={{ color: "var(--g9)", fontSize: 9.5, marginTop: 2, lineHeight: 1.6 }}>
          {t.railBody}
        </div>
      </div>

      <div className="inspector__body">
        {saved && (
          <div className="attach attach--done" data-testid="rail-receipt" role="status">
            {t.railSaved}
          </div>
        )}
        <FieldList
          studio={studio}
          base="/spec"
          specs={PROJECT_FIELDS}
          onSaved={() => setSaved(true)}
        />
      </div>

      <div className="inspector__foot">
        <span style={{ color: "var(--g9)" }}>/spec</span>
        <br />
        <button className="link" onClick={() => studio.actions.setTab("yaml")}>
          {t.revealYaml}
        </button>
      </div>
    </div>
  );
}

export function Inspector({
  studio,
  width,
  scope,
}: {
  studio: Studio;
  width: number;
  scope: CanvasScope;
}) {
  const t = useT();
  const { state, blueprint, actions } = studio;
  const [open, setOpen] = useState(true);
  const [tier, setTier] = useState<"basic" | "advanced">("basic");

  const graph = useMemo(() => (blueprint ? blueprintToGraph(blueprint) : null), [blueprint]);

  const node = useMemo(() => {
    if (!graph || !state.selectedId) return null;
    return graph.nodes.find((item) => item.id === state.selectedId) ?? null;
  }, [graph, state.selectedId]);

  // Read attachment off the graph rather than off `runtime.agent.tools`: a null
  // allowlist grants every declared tool, and the graph already encodes that.
  const toolAttachment = useMemo(() => {
    if (!graph || node?.data.kind !== "tool") return null;
    const primaryId = toLayoutId("primary_agent");
    if (!graph.nodes.some((item) => item.id === primaryId)) return null;
    const attached = graph.edges.some(
      (edge) =>
        edge.source === node.id && edge.target === primaryId && edge.relation === "tool_filter",
    );
    const explicit = Array.isArray(blueprint?.spec.runtime?.agent?.tools);
    return { attached, explicit, primaryId };
  }, [graph, node, blueprint]);

  /** What the primary runtime actually reaches, counted off the saved Blueprint. */
  const runtimeSummary = useMemo(() => {
    if (!blueprint || node?.data.kind !== "runtime_agent") return null;
    const spec = blueprint.spec;
    const declared = spec.tools ?? [];
    const allowlist = spec.runtime?.agent?.tools;
    const extensions = spec.capabilities?.extensions as { mcpServers?: unknown[] } | undefined;
    return {
      tools: Array.isArray(allowlist) ? allowlist.length : declared.length,
      declared: declared.length,
      everyTool: !Array.isArray(allowlist),
      mcp: extensions?.mcpServers?.length ?? 0,
      memory: spec.capabilities?.memory?.backend ?? "none",
      skills: spec.skills?.length ?? 0,
      subagents: spec.subagents?.length ?? 0,
    };
  }, [blueprint, node]);

  if (!open) {
    return (
      <button className="rail hoverable" title={t.expand} onClick={() => setOpen(true)}>
        «
      </button>
    );
  }

  // The project map has no card for provider/memory/MCP, so an empty selection
  // there is the natural home for the runtime configuration rail.
  if (!node && blueprint && scope.kind === "project") {
    return <ProjectRail studio={studio} width={width} onCollapse={() => setOpen(false)} />;
  }

  if (!node || !blueprint) {
    return (
      <div className="inspector" style={{ width }}>
        <div className="inspector__head">
          <div style={{ fontSize: 12, display: "flex", justifyContent: "space-between" }}>
            <b>{t.noSelection}</b>
            <button
              className="btn-outline"
              style={{ padding: "2px 6px", fontSize: 10, fontWeight: 700 }}
              onClick={() => setOpen(false)}
            >
              »
            </button>
          </div>
        </div>
        <div className="inspector__body">
          <div style={{ color: "var(--g6)", lineHeight: 1.7, fontSize: 10 }}>
            {t.noSelectionBody}
          </div>
        </div>
      </div>
    );
  }

  const specs = FIELDS[node.data.kind].filter((spec) => {
    if (spec.tier !== tier) return false;
    if (!spec.when) return true;
    return resolvePointer(blueprint, `${node.data.pointer}/${spec.when.key}`) === spec.when.value;
  });

  return (
    <div className="inspector" style={{ width }} data-testid="inspector">
      <div className="inspector__head">
        <div
          style={{ fontSize: 12, display: "flex", justifyContent: "space-between", gap: 8 }}
        >
          <b>
            {node.data.glyph} {node.data.title}
          </b>
          <span style={{ display: "flex", gap: 6 }}>
            <button
              title={t.deleteTip}
              onClick={() => void actions.removeSelected()}
              style={{
                border: "1px solid var(--red)",
                color: "var(--red)",
                padding: "2px 6px",
                fontSize: 10,
                fontWeight: 700,
              }}
            >
              ✕ {t.delete}
            </button>
            <button
              className="btn-outline"
              style={{ padding: "2px 6px", fontSize: 10, fontWeight: 700 }}
              onClick={() => setOpen(false)}
            >
              »
            </button>
          </span>
        </div>
        <div style={{ color: "var(--g9)", fontSize: 9.5, marginTop: 2 }}>{node.data.sub}</div>
        <div
          className="chip"
          style={{ marginTop: 9, display: "block", padding: "5px 8px", fontSize: 9.5 }}
          title={statusDescription(node.data.status, state.catalog)}
        >
          <BadgeFull status={node.data.status} catalog={state.catalog} />{" "}
          <span style={{ color: "var(--g6)" }}>
            — {statusDescription(node.data.status, state.catalog)}
          </span>
        </div>
      </div>

      <div className="seg">
        <button
          className={`seg__btn${tier === "basic" ? " seg__btn--active" : ""}`}
          onClick={() => setTier("basic")}
        >
          {t.basic}
        </button>
        <button
          className={`seg__btn${tier === "advanced" ? " seg__btn--active" : ""}`}
          onClick={() => setTier("advanced")}
        >
          {t.advanced}
        </button>
      </div>

      <div className="inspector__body">
        {toolAttachment &&
          (toolAttachment.attached ? (
            <div className="attach attach--done" data-testid="tool-attachment" role="status">
              {toolAttachment.explicit ? t.attachedToPrimary : t.attachedByAllowlist}
            </div>
          ) : (
            <div className="attach" data-testid="tool-attachment">
              <button
                className="btn-outline attach__btn"
                onClick={() => void actions.connectNodes(node.id, toolAttachment.primaryId)}
              >
                {t.attachToPrimary}
              </button>
              <div className="attach__note">{t.toolDeclaredHint}</div>
            </div>
          ))}

        {node.data.kind === "subagent" && (
          <div className="attach attach--done" data-testid="subagent-note">
            {t.subagentMemberNote}
          </div>
        )}

        {node.data.kind === "skill" && (
          <div className="attach attach--done" data-testid="skill-note">
            {t.skillDiscoveredNote}
          </div>
        )}

        {runtimeSummary && (
          <div className="attach attach--done" data-testid="runtime-summary">
            <div>
              {t.summaryTools}: {runtimeSummary.tools}
              {runtimeSummary.everyTool ? ` ${t.summaryEveryTool}` : ""}
            </div>
            <div>
              {t.summaryMcp}: {runtimeSummary.mcp}
            </div>
            <div>
              {t.summaryMemory}: {runtimeSummary.memory}
            </div>
            <div>
              {t.summarySkills}: {runtimeSummary.skills}
            </div>
            <div>
              {t.summarySubagents}: {runtimeSummary.subagents}
            </div>
          </div>
        )}

        {tier === "advanced" && (
          <div
            style={{
              border: "1px dotted var(--ink)",
              padding: "6px 9px",
              fontSize: 9,
              color: "var(--g6)",
              marginBottom: 12,
              lineHeight: 1.6,
            }}
          >
            {t.advNote}
          </div>
        )}

        <FieldList studio={studio} base={node.data.pointer} specs={specs} />
      </div>

      <div className="inspector__foot">
        <span style={{ color: "var(--g9)" }}>{node.data.pointer}</span>
        <br />
        <button className="link" onClick={() => actions.setTab("yaml")}>
          {t.revealYaml}
        </button>
      </div>
    </div>
  );
}

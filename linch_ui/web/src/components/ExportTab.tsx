import { useState } from "react";

import { StudioApiError, type StudioApi } from "../api/client";
import { useT } from "../i18n";
import { countBy, hasErrors, markerColor, type Marker } from "../state/derive";
import type { Studio } from "../state/useStudio";

interface Check {
  mark: Marker;
  text: string;
  tag: string;
}

/** Readiness rows derived from real diagnostics — never from canned fixtures. */
function checklist(studio: Studio, t: ReturnType<typeof useT>): Check[] {
  const { state, blueprint } = studio;
  const errors = countBy(state.diagnostics, "error");
  const warnings = countBy(state.diagnostics, "warning");

  const envNames = new Set<string>();
  const walk = (value: unknown) => {
    if (Array.isArray(value)) return value.forEach(walk);
    if (value && typeof value === "object") {
      for (const [key, item] of Object.entries(value)) {
        if (key.endsWith("Env") && typeof item === "string") envNames.add(item);
        else walk(item);
      }
    }
  };
  walk(blueprint);

  const rows: Check[] = [
    errors > 0
      ? { mark: "[xx]", text: `validation — ${errors} blocking error(s)`, tag: "blocked" }
      : { mark: "[ok]", text: "validation — no blocking errors", tag: "pass" },
    warnings > 0
      ? { mark: "[!!]", text: `${warnings} warning(s) — review before export`, tag: "todo" }
      : { mark: "[ok]", text: "no warnings", tag: "pass" },
  ];

  rows.push(
    envNames.size > 0
      ? {
          mark: "[!!]",
          text: `env-var names: ${[...envNames].sort().join(", ")}`,
          tag: `${envNames.size} names`,
        }
      : { mark: "[ok]", text: "no environment variables referenced", tag: "pass" },
  );
  rows.push({ mark: "[!!]", text: t.expDeployNote, tag: "todo" });
  return rows;
}

export function ExportTab({ studio, api }: { studio: Studio; api: StudioApi }) {
  const t = useT();
  const { state, actions } = studio;
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<{ title: string; path: string } | null>(null);

  const blocked = hasErrors(state.diagnostics) || !state.structurallyValid;
  const projectId = state.doc?.id;

  const exportDir = async () => {
    if (!projectId || !target.trim()) return;
    setBusy(true);
    try {
      const receipt = await api.exportDirectory(projectId, target.trim());
      setDone({ title: t.exportedDir, path: receipt.target });
    } catch (error) {
      if (error instanceof StudioApiError) {
        actions.toast("[xx]", error.message);
      }
    } finally {
      setBusy(false);
    }
  };

  const exportZip = async () => {
    if (!projectId) return;
    setBusy(true);
    try {
      const result = await api.exportZip(projectId);
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename;
      anchor.click();
      URL.revokeObjectURL(url);
      setDone({ title: t.exportedZip, path: result.filename });
    } catch (error) {
      if (error instanceof StudioApiError) {
        actions.toast("[xx]", error.message);
      }
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <div className="export">
        <div className="export__inner">
          <div className="export__done">
            <div style={{ fontSize: 12, marginBottom: 8 }}>
              <b>[ok] {done.title}</b>
            </div>
            <div
              style={{
                border: "1px dotted var(--b9)",
                padding: "7px 10px",
                color: "var(--g6)",
                marginBottom: 10,
              }}
            >
              {done.path}
            </div>
            <div style={{ color: "var(--g6)", lineHeight: 1.7, marginBottom: 12 }}>
              {t.exportDoneNote}
            </div>
            <button className="btn-outline" onClick={() => setDone(null)}>
              {t.backExport}
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="export">
      <div className="export__inner">
        <div className="export__box">
          <div className="export__boxhead">
            <span>{t.readiness}</span>
            <span
              style={{ fontWeight: 400, color: blocked ? "var(--red)" : "var(--ink)" }}
            >
              {blocked ? t.exportBlocked : t.exportOk}
            </span>
          </div>
          <div className="export__rows">
            {checklist(studio, t).map((row, index) => (
              <div className="export__row" key={index}>
                <span>
                  <b style={{ color: markerColor(row.mark) }}>{row.mark}</b> {row.text}
                </span>
                <span style={{ color: "var(--g9)" }}>{row.tag}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="export__callout">
          <b>{t.noOverwrite}</b> {t.noOverwriteBody}
        </div>

        <div className="export__cli">
          <span style={{ color: "var(--g9)" }}>{t.cliLabel}</span>
          {"\n"}$ linch-studio export ./designs/{projectId}/linch-studio.yaml --out ../
          {projectId}
        </div>

        <div className="export__cards">
          <div className="export__card">
            <div style={{ fontWeight: 700, marginBottom: 6 }}>{t.expDir}</div>
            <label className="modal__label" htmlFor="export-target">
              {t.targetLabel}
            </label>
            <input
              id="export-target"
              className="export__pathbox"
              value={target}
              onChange={(event) => setTarget(event.target.value)}
              placeholder={`../${projectId ?? "project"}`}
              style={{ border: "1px dotted var(--b9)" }}
            />
            <div className="field__desc" style={{ marginBottom: 10 }}>
              {t.targetHint}
            </div>
            <button
              className="export__cardbtn"
              disabled={blocked || busy || !target.trim()}
              style={{
                background: blocked || !target.trim() ? "var(--mut)" : "var(--ink)",
                color: blocked || !target.trim() ? "var(--g9)" : "var(--bg)",
              }}
              onClick={() => void exportDir()}
            >
              {busy ? t.exporting : t.expDirBtn}
            </button>
          </div>

          <div className="export__card">
            <div style={{ fontWeight: 700, marginBottom: 6 }}>{t.expZip}</div>
            <div className="export__pathbox">
              {projectId}.zip · {t.byteStable}
            </div>
            <button
              className="export__cardbtn"
              disabled={blocked || busy}
              style={{ color: blocked ? "var(--g9)" : "var(--ink)" }}
              onClick={() => void exportZip()}
            >
              {busy ? t.exporting : t.expZipBtn}
            </button>
          </div>
        </div>

        {blocked && (
          <div style={{ marginTop: 12, textAlign: "center", fontSize: 10 }}>
            <b style={{ color: "var(--red)" }}>[xx]</b> {t.expBlocked}
          </div>
        )}
      </div>
    </div>
  );
}

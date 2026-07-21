import { useEffect, useMemo, useState } from "react";

import { StudioApiError, type StudioApi } from "../api/client";
import type { ExportPreviewResponse, GeneratedFilePreview } from "../api/types";
import { useT } from "../i18n";
import { hasErrors } from "../state/derive";
import type { Studio } from "../state/useStudio";

interface Row {
  depth: number;
  name: string;
  path?: string;
  todo?: boolean;
}

/** A TODO seam raises an error at runtime; it never fakes success. */
function isTodo(file: GeneratedFilePreview): boolean {
  return /\bTODO\b/.test(file.content) || /NotImplementedError/.test(file.content);
}

function buildTree(files: GeneratedFilePreview[], root: string): Row[] {
  const rows: Row[] = [{ depth: 0, name: `${root}/` }];
  const seen = new Set<string>();
  for (const file of [...files].sort((a, b) => a.path.localeCompare(b.path))) {
    const parts = file.path.split("/");
    parts.forEach((part, index) => {
      const prefix = parts.slice(0, index + 1).join("/");
      if (index === parts.length - 1) {
        rows.push({ depth: index + 1, name: part, path: file.path, todo: isTodo(file) });
        return;
      }
      if (seen.has(prefix)) return;
      seen.add(prefix);
      rows.push({ depth: index + 1, name: `${part}/` });
    });
  }
  return rows;
}

export function FilesTab({ studio, api }: { studio: Studio; api: StudioApi }) {
  const t = useT();
  const { state, actions } = studio;
  const [preview, setPreview] = useState<ExportPreviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const blocked = hasErrors(state.diagnostics) || !state.structurallyValid;
  const projectId = state.doc?.id;

  useEffect(() => {
    if (!projectId || blocked) {
      setPreview(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const result = await api.previewExport(projectId);
        if (cancelled) return;
        setPreview(result);
        const todo = result.files.find(isTodo);
        setOpen((current) => current ?? todo?.path ?? result.files[0]?.path ?? null);
      } catch (caught) {
        if (cancelled) return;
        setError(caught instanceof StudioApiError ? caught.message : String(caught));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, projectId, blocked, state.doc?.digest]);

  const tree = useMemo(
    () => (preview ? buildTree(preview.files, state.doc?.id ?? "project") : []),
    [preview, state.doc?.id],
  );

  const current = preview?.files.find((file) => file.path === open) ?? null;

  if (blocked) {
    return (
      <div className="center">
        <div className="empty__glyph">⊘</div>
        <b>{t.filesBlockedTitle}</b>
        <div className="empty__body">{t.filesBlockedBody}</div>
        <button className="btn-outline" onClick={() => actions.setTab("diagnostics")}>
          {t.goDiag}
        </button>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="center">
        <span style={{ color: "var(--g9)" }}>
          {t.loadingFiles}
          <span className="blink">_</span>
        </span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="center">
        <b style={{ color: "var(--red)" }}>[xx]</b>
        <div className="empty__body">{error}</div>
      </div>
    );
  }

  if (!preview) return <div className="center" />;

  return (
    <div className="files">
      <div className="files__tree">
        {tree.map((row, index) => {
          const active = row.path && row.path === open;
          return row.path ? (
            <button
              key={`${row.path}-${index}`}
              className={`files__row hoverable${active ? " files__row--active" : ""}`}
              style={{ paddingLeft: 14 + row.depth * 14 }}
              onClick={() => setOpen(row.path!)}
            >
              <span>{row.name}</span>
              {row.todo && (
                <b
                  style={{
                    fontSize: 8.5,
                    alignSelf: "center",
                    color: active ? "var(--bg)" : "var(--yel)",
                  }}
                >
                  [TODO]
                </b>
              )}
            </button>
          ) : (
            <div
              key={`${row.name}-${index}`}
              className="files__row files__row--dir"
              style={{ paddingLeft: 14 + row.depth * 14 }}
            >
              <span>{row.name}</span>
            </div>
          );
        })}
      </div>

      <div className="files__view">
        {current && (
          <>
            <div className="files__path">
              <span>
                <b>{current.path}</b> ·{" "}
                <b style={{ color: isTodo(current) ? "var(--yel)" : "var(--ink)" }}>
                  {isTodo(current) ? t.skeleton : t.generated}
                </b>
              </span>
              <span style={{ color: "var(--g9)" }}>sha256 {current.sha256.slice(0, 12)}</span>
            </div>
            <div className="files__prov">
              {t.provenance} {current.capabilityId} · {current.size} bytes
            </div>
            <pre className="files__code">
              {current.content.split("\n").map((line, index) => (
                <div
                  className="code__line"
                  key={index}
                  style={{ background: /TODO/.test(line) ? "var(--hl)" : undefined }}
                >
                  <span className="code__gutter">{index + 1}</span>
                  <span className="code__text">{line}</span>
                </div>
              ))}
            </pre>
          </>
        )}
        <div className="files__foot">{t.filesFoot}</div>
      </div>
    </div>
  );
}

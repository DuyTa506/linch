import { useState } from "react";

import type { ProjectSummary } from "../api/types";
import { useT } from "../i18n";
import { SHARED_TEMPLATE_CHOICES, type StudioTemplateId } from "../model/palette";
import type { Studio } from "../state/useStudio";

function stateOf(project: ProjectSummary): { mark: string; text: string; color: string; weight: number } {
  if (project.exportReady) {
    return { mark: "[ok]", text: "valid", color: "var(--ink)", weight: 700 };
  }
  return { mark: "[xx]", text: "blocked", color: "var(--red)", weight: 400 };
}

/** Relative age for the MOD column, from the API's `updatedAt`. */
function age(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function CreateDialog({
  onCancel,
  onCreate,
}: {
  onCancel: () => void;
  onCreate: (id: string, title: string, template: StudioTemplateId) => void;
}) {
  const t = useT();
  const [id, setId] = useState("");
  const [title, setTitle] = useState("");
  const [template, setTemplate] = useState<StudioTemplateId>("agent");
  const valid = /^[a-z][a-z0-9_]{0,63}$/.test(id);

  return (
    <div className="modal" role="dialog" aria-label={t.createTitle}>
      <button className="overlay__scrim" onClick={onCancel} aria-label={t.cancel} />
      <div className="modal__panel" style={{ margin: "56px auto 0" }}>
        <div className="modal__head">
          <b>{t.createTitle}</b>
          <button onClick={onCancel} aria-label={t.cancel}>
            ✕
          </button>
        </div>
        <div className="modal__body">
          <div>
            <div className="modal__label">{t.createIdLabel}</div>
            <input
              autoFocus
              value={id}
              onChange={(event) => setId(event.target.value)}
              placeholder="nightly_review"
              aria-label={t.createIdLabel}
              style={{ width: "100%" }}
            />
            <div className="field__desc">{t.createIdHint}</div>
          </div>
          <div>
            <div className="modal__label">{t.createTitleLabel}</div>
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Nightly Review"
              aria-label={t.createTitleLabel}
              style={{ width: "100%" }}
            />
          </div>
          <div>
            <div className="modal__label">{t.createTemplateLabel}</div>
            <select
              value={template}
              onChange={(event) => setTemplate(event.target.value as StudioTemplateId)}
              aria-label={t.createTemplateLabel}
              style={{ width: "100%" }}
            >
              {SHARED_TEMPLATE_CHOICES.map((choice) => (
                <option key={choice.id} value={choice.id}>
                  {choice.label}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="modal__foot">
          <button className="link" onClick={onCancel}>
            {t.cancel}
          </button>
          <button
            className="btn-solid"
            disabled={!valid}
            onClick={() => onCreate(id, title.trim() || id, template)}
          >
            {t.create}
          </button>
        </div>
      </div>
    </div>
  );
}

export function Home({
  studio,
  onOpenDocumentation,
}: {
  studio: Studio;
  onOpenDocumentation: () => void;
}) {
  const t = useT();
  const { state, actions } = studio;
  const [creating, setCreating] = useState(false);

  const create = async (id: string, title: string, template: StudioTemplateId) => {
    const ok = await actions.createProject(id, title, template);
    if (ok) setCreating(false);
  };

  return (
    <div className="home">
      <div className="home__inner">
        <div className="home__head">
          <div style={{ fontSize: 13, fontWeight: 700 }}>
            {t.brand}
            <span style={{ color: "var(--g9)" }}>{t.homeSub}</span>
          </div>
          <div style={{ display: "flex", gap: 16, fontSize: 10.5, alignItems: "center" }}>
            <span style={{ color: "var(--g9)" }}>{t.local}</span>
            <button className="btn-solid" onClick={() => setCreating(true)}>
              {t.newBlueprint}
            </button>
          </div>
        </div>

        <div className="home__flow">{t.flowLine}</div>

        <section className="home__guide" aria-labelledby="getting-started-title">
          <div>
            <div className="home__guide-eyebrow">{t.docs.homeEyebrow}</div>
            <h1 id="getting-started-title">{t.docs.homeTitle}</h1>
            <p>{t.docs.homeBody}</p>
          </div>
          <button className="btn-solid" onClick={onOpenDocumentation}>
            {t.docs.homeCta} →
          </button>
        </section>

        <div style={{ fontSize: 11 }}>
          <div className="home__row home__thead">
            <span>{t.colName}</span>
            <span>{t.colId}</span>
            <span>{t.colModel}</span>
            <span>{t.colState}</span>
            <span>{t.colExport}</span>
            <span style={{ textAlign: "right" }}>{t.colMod}</span>
          </div>

          {state.projectsLoading && (
            <div style={{ padding: "18px 0", color: "var(--g9)" }}>
              {t.loadingProjects}
              <span className="blink">_</span>
            </div>
          )}

          {!state.projectsLoading && state.projects.length === 0 && (
            <div style={{ padding: "34px 0", textAlign: "center" }}>
              <b>{t.noProjects}</b>
              <div
                style={{
                  color: "var(--g6)",
                  fontSize: 10.5,
                  lineHeight: 1.7,
                  maxWidth: 380,
                  margin: "6px auto 0",
                }}
              >
                {t.noProjectsBody}
              </div>
            </div>
          )}

          {state.projects.map((project) => {
            const status = stateOf(project);
            return (
              <button
                key={project.id}
                className="home__row home__trow hoverable"
                onClick={() => void actions.openProject(project.id)}
              >
                <span style={{ fontWeight: 700, textAlign: "left" }}>▸ {project.title}</span>
                <span style={{ color: "var(--g9)", textAlign: "left" }}>{project.id}</span>
                <span style={{ textAlign: "left" }}>{project.model ?? "—"}</span>
                <span style={{ color: status.color, fontWeight: status.weight, textAlign: "left" }}>
                  {status.mark} {status.text}
                </span>
                <span
                  style={{ color: status.color, fontWeight: status.weight, textAlign: "left" }}
                >
                  {project.exportReady ? "ready" : "blocked"}
                </span>
                <span style={{ color: "var(--g9)", textAlign: "right" }}>
                  {age(project.updatedAt)}
                </span>
              </button>
            );
          })}

          {!state.projectsLoading && (
            <button
              onClick={() => setCreating(true)}
              style={{ color: "var(--g9)", padding: "12px 0", fontSize: 11 }}
            >
              {t.newEmpty}
              <span className="blink">_</span>
            </button>
          )}
        </div>

        <div className="home__foot">
          <span>{t.homeFootLeft(state.projects.length)}</span>
        </div>
      </div>

      {creating && <CreateDialog onCancel={() => setCreating(false)} onCreate={create} />}
    </div>
  );
}

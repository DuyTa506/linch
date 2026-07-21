import { yaml as yamlLang } from "@codemirror/lang-yaml";
import { EditorState } from "@codemirror/state";
import { EditorView, lineNumbers } from "@codemirror/view";
import { useEffect, useRef } from "react";

import { useT } from "../i18n";
import { hasErrors, pointerToLine } from "../state/derive";
import type { Studio } from "../state/useStudio";

export function YamlTab({ studio }: { studio: Studio }) {
  const t = useT();
  const { state, actions } = studio;
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView>();

  useEffect(() => {
    if (!host.current || view.current) return;
    view.current = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: state.buffer,
        extensions: [
          lineNumbers(),
          yamlLang(),
          EditorView.lineWrapping,
          EditorView.updateListener.of((update) => {
            if (update.docChanged) actions.setBuffer(update.state.doc.toString());
          }),
          EditorView.theme({
            "&": { backgroundColor: "var(--bg)", color: "var(--ink)" },
            ".cm-gutters": {
              backgroundColor: "var(--bg)",
              color: "var(--fnt)",
              border: "none",
            },
            ".cm-activeLine": { backgroundColor: "var(--mut)" },
            ".cm-activeLineGutter": { backgroundColor: "var(--mut)", color: "var(--ink)" },
            ".cm-cursor": { borderLeftColor: "var(--ink)" },
            ".cm-selectionBackground, ::selection": { backgroundColor: "var(--dot)" },
          }),
        ],
      }),
    });
    return () => {
      view.current?.destroy();
      view.current = undefined;
    };
    // Mount once; buffer sync is handled below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Pull external buffer changes (revert, save, proposal accept) into the editor.
  useEffect(() => {
    const editor = view.current;
    if (!editor) return;
    if (editor.state.doc.toString() === state.buffer) return;
    editor.dispatch({
      changes: { from: 0, to: editor.state.doc.length, insert: state.buffer },
    });
  }, [state.buffer]);

  const errored = hasErrors(state.diagnostics);
  const footer = !state.structurallyValid
    ? { text: t.yamlErrFoot, color: "var(--red)" }
    : errored
      ? { text: t.yamlDraftFoot, color: "var(--yel)" }
      : { text: t.yamlOk, color: "var(--ink)" };

  const note = !state.structurallyValid
    ? t.yamlBuffer
    : errored
      ? t.yamlDraft
      : t.yamlSynced;

  const canSave = state.bufferDirty && state.structurallyValid && state.saveState !== "saving";

  const anchored = state.diagnostics
    .map((item) => pointerToLine(state.buffer, item.path))
    .filter((line): line is number => line !== null);

  return (
    <div className="yaml">
      <div className="yaml__head">
        <span>
          <b>{t.yamlFile}</b>
          <span style={{ color: "var(--g9)" }}> · {note}</span>
          {anchored.length > 0 && (
            <span style={{ color: "var(--g9)" }}> · lines {anchored.join(", ")}</span>
          )}
        </span>
        <span style={{ color: "var(--g9)" }} title={t.commentTip}>
          {t.commentNote}
        </span>
      </div>

      <div className="yaml__editor" ref={host} data-testid="yaml-editor" />

      <div className="yaml__foot">
        <span style={{ color: footer.color }}>{footer.text}</span>
        <span style={{ display: "flex", gap: 10, alignItems: "center" }}>
          {state.bufferDirty && (
            <button className="link" onClick={actions.revertBuffer}>
              {t.revert}
            </button>
          )}
          <button className="btn-solid" disabled={!canSave} onClick={() => void actions.save()}>
            {state.saveState === "saving" ? t.saving : t.saveBlueprint}
          </button>
        </span>
      </div>
    </div>
  );
}

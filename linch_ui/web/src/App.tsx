import { useEffect, useState } from "react";

import type { StudioApi } from "./api/client";
import { Documentation } from "./components/Documentation";
import { Home } from "./components/Home";
import { Settings, loadPrefs, savePrefs, type Prefs } from "./components/Settings";
import { Studio } from "./components/Studio";
import { SupportDrawer } from "./components/SupportDrawer";
import { I18nContext, dictionaries } from "./i18n";
import { useStudio } from "./state/useStudio";
import { useSupport } from "./state/useSupport";

function Toast({ studio }: { studio: ReturnType<typeof useStudio> }) {
  const { state, actions } = studio;
  useEffect(() => {
    if (!state.toast) return;
    const timer = setTimeout(actions.dismissToast, 6000);
    return () => clearTimeout(timer);
  }, [state.toast, actions]);

  if (!state.toast) return null;
  const color =
    state.toast.marker === "[xx]"
      ? "var(--red)"
      : state.toast.marker === "[!!]"
        ? "var(--yel)"
        : "var(--ink)";
  return (
    <div
      role="status"
      data-testid="toast"
      style={{
        position: "absolute",
        bottom: 42,
        left: "50%",
        transform: "translateX(-50%)",
        background: "var(--card)",
        border: "1px solid var(--ink)",
        boxShadow: "var(--shdw)",
        padding: "8px 14px",
        fontSize: 10.5,
        zIndex: 70,
        maxWidth: "70%",
      }}
    >
      <b style={{ color }}>{state.toast.marker}</b> {state.toast.text}
      <button className="link" style={{ marginLeft: 12 }} onClick={actions.dismissToast}>
        dismiss
      </button>
    </div>
  );
}

export function App({ api }: { api: StudioApi }) {
  const studio = useStudio(api);
  const support = useSupport(api);
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [supportOpen, setSupportOpen] = useState(false);
  const [documentationOpen, setDocumentationOpen] = useState(
    () => window.location.pathname === "/docs" || window.location.pathname.startsWith("/docs/"),
  );

  useEffect(() => {
    document.documentElement.dataset.theme = prefs.dark ? "dark" : "light";
    savePrefs(prefs);
  }, [prefs]);

  useEffect(() => {
    const syncPath = () => {
      setDocumentationOpen(
        window.location.pathname === "/docs" || window.location.pathname.startsWith("/docs/"),
      );
    };
    window.addEventListener("popstate", syncPath);
    return () => window.removeEventListener("popstate", syncPath);
  }, []);

  const { state } = studio;
  const openDocumentation = () => {
    if (window.location.pathname !== "/docs") {
      window.history.pushState({}, "", "/docs");
    }
    setDocumentationOpen(true);
  };
  const closeDocumentation = () => {
    window.history.pushState({}, "", "/");
    setDocumentationOpen(false);
  };

  return (
    <I18nContext.Provider value={dictionaries[prefs.lang]}>
      <div className="app">
        {!state.ready && (
          <div className="center">
            <span style={{ color: "var(--g9)" }}>
              {dictionaries[prefs.lang].loading}
              <span className="blink">_</span>
            </span>
          </div>
        )}

        {state.ready && state.bootError && (
          <div className="center">
            <b style={{ color: "var(--red)" }}>[xx] {dictionaries[prefs.lang].loadFailed}</b>
            <div className="empty__body">{state.bootError}</div>
            <button className="btn-outline" onClick={() => window.location.reload()}>
              {dictionaries[prefs.lang].retry}
            </button>
          </div>
        )}

        {state.ready && !state.bootError && documentationOpen && (
          <>
            <div className="topbar">
              <button className="topbar__brand" onClick={closeDocumentation}>
                {dictionaries[prefs.lang].brand}
              </button>
              <span style={{ color: "var(--g9)" }}>:: documentation</span>
              <span style={{ flex: 1 }} />
              <button className="link" onClick={closeDocumentation}>
                ← {dictionaries[prefs.lang].docs.back}
              </button>
              <button className="link" onClick={() => setSupportOpen(true)}>
                ✻ Support
              </button>
              <button
                className="btn-outline hoverable"
                style={{ padding: "3px 9px", fontSize: 11 }}
                title={dictionaries[prefs.lang].settings}
                onClick={() => setSettingsOpen(true)}
              >
                ⚙ {prefs.lang.toUpperCase()}
              </button>
            </div>
            <Documentation onBack={closeDocumentation} />
          </>
        )}

        {state.ready && !state.bootError && !documentationOpen && !state.doc && (
          <>
            <div className="topbar">
              <button className="topbar__brand" onClick={closeDocumentation}>
                {dictionaries[prefs.lang].brand}
              </button>
              <span style={{ flex: 1 }} />
              <button className="link" onClick={openDocumentation}>
                ? {dictionaries[prefs.lang].docs.navLabel.toLowerCase()}
              </button>
              <button className="link" onClick={() => setSupportOpen(true)}>
                ✻ Support
              </button>
              <button
                className="btn-outline hoverable"
                style={{ padding: "3px 9px", fontSize: 11 }}
                title={dictionaries[prefs.lang].settings}
                onClick={() => setSettingsOpen(true)}
              >
                ⚙ {prefs.lang.toUpperCase()}
              </button>
            </div>
            <Home studio={studio} onOpenDocumentation={openDocumentation} />
          </>
        )}

        {state.ready && !state.bootError && !documentationOpen && state.doc && (
          <Studio
            studio={studio}
            api={api}
            lang={prefs.lang}
            onOpenAi={() => setSupportOpen(true)}
            onOpenDocumentation={openDocumentation}
            onOpenSettings={() => setSettingsOpen(true)}
          />
        )}

        {settingsOpen && (
          <Settings prefs={prefs} onChange={setPrefs} onClose={() => setSettingsOpen(false)} />
        )}
        {supportOpen && (
          <SupportDrawer
            support={support}
            studio={studio}
            projectId={state.doc?.id}
            onClose={() => setSupportOpen(false)}
          />
        )}
        <Toast studio={studio} />
      </div>
    </I18nContext.Provider>
  );
}

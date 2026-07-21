import { useT, type Lang } from "../i18n";

export interface Prefs {
  lang: Lang;
  dark: boolean;
}

export const PREFS_KEY = "linch_studio_cfg";

export function loadPrefs(): Prefs {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return { lang: "en", dark: false };
    const parsed = JSON.parse(raw) as Partial<Prefs>;
    return {
      lang: parsed.lang === "vi" ? "vi" : "en",
      dark: parsed.dark === true,
    };
  } catch {
    return { lang: "en", dark: false };
  }
}

export function savePrefs(prefs: Prefs): void {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // A blocked localStorage must not break the editor.
  }
}

export function Settings({
  prefs,
  onChange,
  onClose,
}: {
  prefs: Prefs;
  onChange: (prefs: Prefs) => void;
  onClose: () => void;
}) {
  const t = useT();

  return (
    <div className="modal" role="dialog" aria-label={t.settings}>
      <button className="overlay__scrim" onClick={onClose} aria-label="close" />
      <div className="modal__panel">
        <div className="modal__head">
          <b>⚙ {t.settings}</b>
          <button onClick={onClose} aria-label="close">
            ✕
          </button>
        </div>
        <div className="modal__body">
          <div>
            <div className="modal__label">{t.language}</div>
            <div className="segbar">
              <button
                className={`segbar__btn${prefs.lang === "en" ? " segbar__btn--active" : ""}`}
                onClick={() => onChange({ ...prefs, lang: "en" })}
              >
                EN · english
              </button>
              <button
                className={`segbar__btn${prefs.lang === "vi" ? " segbar__btn--active" : ""}`}
                onClick={() => onChange({ ...prefs, lang: "vi" })}
              >
                VI · tiếng việt
              </button>
            </div>
          </div>
          <div>
            <div className="modal__label">{t.theme}</div>
            <div className="segbar">
              <button
                className={`segbar__btn${!prefs.dark ? " segbar__btn--active" : ""}`}
                onClick={() => onChange({ ...prefs, dark: false })}
              >
                {t.light}
              </button>
              <button
                className={`segbar__btn${prefs.dark ? " segbar__btn--active" : ""}`}
                onClick={() => onChange({ ...prefs, dark: true })}
              >
                {t.dark}
              </button>
            </div>
          </div>
        </div>
        <div className="modal__foot">
          <span style={{ color: "var(--g9)", fontSize: 9.5 }}>{t.savedLocal}</span>
          <button className="btn-solid" style={{ padding: "6px 16px" }} onClick={onClose}>
            {t.save}
          </button>
        </div>
      </div>
    </div>
  );
}

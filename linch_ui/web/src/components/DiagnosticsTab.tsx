import type { Diagnostic } from "../api/types";
import { useT } from "../i18n";
import { markerColor, severityMarker } from "../state/derive";
import type { Studio } from "../state/useStudio";

function style(severity: Diagnostic["severity"]) {
  if (severity === "error") {
    return {
      leftBorder: "1px solid var(--red)",
      headBg: "var(--red)",
      headColor: "var(--bg)",
      sevColor: "var(--bg)",
      headBorder: "none",
    };
  }
  return {
    leftBorder: `4px solid ${severity === "warning" ? "var(--yel)" : "var(--ink)"}`,
    headBg: "transparent",
    headColor: "var(--ink)",
    sevColor: severity === "warning" ? "var(--yel)" : "var(--ink)",
    headBorder: "1px dotted var(--dot)",
  };
}

const LABEL: Record<Diagnostic["severity"], string> = {
  error: "ERROR",
  warning: "WARN",
  info: "INFO",
};

export function DiagnosticsTab({ studio }: { studio: Studio }) {
  const t = useT();
  const { state, actions } = studio;

  if (state.diagnostics.length === 0) {
    return (
      <div className="center">
        <b>{t.noDiagnostics}</b>
        <div style={{ color: "var(--g6)", fontSize: 10.5 }}>{t.noDiagnosticsBody}</div>
      </div>
    );
  }

  return (
    <div className="diag">
      <div className="diag__inner">
        {state.diagnostics.map((item, index) => {
          const skin = style(item.severity);
          return (
            <div
              className="diag__card"
              key={`${item.code}-${item.path}-${index}`}
              style={{ borderLeft: skin.leftBorder }}
            >
              <div
                className="diag__head"
                style={{
                  background: skin.headBg,
                  color: skin.headColor,
                  borderBottom: skin.headBorder,
                }}
              >
                <b style={{ color: skin.sevColor }}>
                  {severityMarker(item.severity)} {LABEL[item.severity]}
                </b>
                <span style={{ opacity: 0.75 }}>{item.code}</span>
                <span style={{ flex: 1 }} />
                <span style={{ opacity: 0.6, fontSize: 9.5 }}>{item.path}</span>
              </div>
              <div className="diag__body">
                {item.message}
                <br />
                <span style={{ color: "var(--g6)" }}>
                  {t.fix} {item.remediation}
                </span>
                <br />
                <button className="link" onClick={() => actions.setTab("yaml")}>
                  {t.revealYaml}
                </button>
              </div>
            </div>
          );
        })}
        <div className="diag__foot">
          <b style={{ color: markerColor("[ok]") }}>[ok]</b> {t.diagFoot.replace("[ok] ", "")}
        </div>
      </div>
    </div>
  );
}

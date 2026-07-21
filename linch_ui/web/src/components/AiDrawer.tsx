import { useEffect, useRef, useState } from "react";

import type { ProposalResponse } from "../api/types";
import { useT } from "../i18n";
import { markerColor, severityMarker } from "../state/derive";
import type { Studio, TranscriptQuestion, TranscriptToolCall } from "../state/useStudio";

/** Fields the authoring guard refuses to let AI introduce (authoring/guards.py). */
const MANUAL_ONLY = [
  "redaction regex",
  "local mcp command/args",
  "permission rules",
];

const EXAMPLES = [
  "Create a code-reviewer subagent.",
  "Add a researcher and verifier, then build a fan-out/fan-in workflow.",
  "Create a nightly recurring review with explicit safety limits.",
];

function shortValue(value: unknown): string {
  const text = JSON.stringify(value) ?? "null";
  return text.length > 42 ? `${text.slice(0, 39)}…` : text;
}

function Diff({ proposal }: { proposal: ProposalResponse }) {
  const t = useT();
  if (proposal.semanticDiff.length === 0) return null;
  return (
    <div style={{ padding: "6px 10px", lineHeight: 1.9 }}>
      <div style={{ fontSize: 9, color: "var(--g9)", letterSpacing: "0.08em" }}>
        {t.semanticDiffHead}
      </div>
      {proposal.semanticDiff.map((entry, index) => (
        <div key={index}>
          <b>
            {entry.operation === "add" ? "+" : entry.operation === "remove" ? "−" : "~"}
          </b>{" "}
          {entry.path}
          {entry.operation === "replace" && (
            <span style={{ color: "var(--g6)" }}>
              {" "}
              {shortValue(entry.before)} → {shortValue(entry.after)}
            </span>
          )}
          {entry.operation === "add" && entry.after !== undefined && entry.after !== null && (
            <span style={{ color: "var(--g6)" }}> = {shortValue(entry.after)}</span>
          )}
          {entry.operation === "remove" && entry.before !== undefined && entry.before !== null && (
            <span style={{ color: "var(--g6)" }}> ({shortValue(entry.before)})</span>
          )}
        </div>
      ))}
    </div>
  );
}

/** Dimmed, collapsed reasoning trace; expanding it is opt-in. */
function Thinking({ trace }: { trace?: string | null }) {
  const t = useT();
  if (!trace) return null;
  return (
    <details className="msg__thinking">
      <summary>{t.aiThinkingLabel}</summary>
      <div className="msg__thinking-body">{trace}</div>
    </details>
  );
}

/** The in-flight reasoning trace: open (not a drop-down) and pinned to its tail. */
function StreamingTrace({ text }: { text: string }) {
  const t = useT();
  const bodyRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const node = bodyRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [text]);
  return (
    <div className="msg msg--agent msg--stream">
      <div className="msg__stream-label">
        <span className="blink">▪</span> {t.aiThinkingLive}
      </div>
      {text && (
        <div ref={bodyRef} className="msg__stream-body">
          {text}
        </div>
      )}
    </div>
  );
}

/** One tool call as a single compact line; expands only when there is detail to show. */
function ToolCallRow({ call }: { call: TranscriptToolCall }) {
  const t = useT();
  const rowClass =
    call.status === "error" ? "msg__tool-call msg__tool-call--error" : "msg__tool-call";
  const line =
    call.status === "running" ? (
      <>
        <span className="blink">▪</span> {call.summary} — {t.aiToolRunning}
      </>
    ) : (
      <>
        <b style={{ color: markerColor(call.status === "error" ? "[xx]" : "[ok]") }}>
          {call.status === "error" ? "[xx]" : "[ok]"}
        </b>{" "}
        {call.summary} — {call.resultSummary ?? call.summary}
      </>
    );
  if (!call.detail) {
    return <div className={rowClass}>{line}</div>;
  }
  return (
    <details className={rowClass}>
      <summary>{line}</summary>
      <div className="msg__tool-call-body">{call.detail}</div>
    </details>
  );
}

/** The compact, Claude-Code-style tool-call trace for one turn (or the live run). */
function ToolCalls({ calls }: { calls?: TranscriptToolCall[] | null }) {
  if (!calls || calls.length === 0) return null;
  return (
    <div className="msg__tool-calls">
      {calls.map((call) => (
        <ToolCallRow key={call.toolUseId} call={call} />
      ))}
    </div>
  );
}

const OTHER_OPTION = "__other__";

/**
 * AskUserQuestion-style answer form: the agent's concrete options as buttons,
 * plus one UI-owned free-form option per question. All answers travel back as
 * a single composed user message.
 */
function QuestionForm({
  questions,
  busy,
  onSubmit,
}: {
  questions: TranscriptQuestion[];
  busy: boolean;
  onSubmit: (message: string) => void;
}) {
  const t = useT();
  const [choices, setChoices] = useState<Record<number, string>>({});
  const [custom, setCustom] = useState<Record<number, string>>({});

  const answerFor = (index: number): string => {
    const choice = choices[index];
    if (choice === OTHER_OPTION) return (custom[index] ?? "").trim();
    return choice ?? "";
  };
  const complete = questions.every((_, index) => answerFor(index).length > 0);
  const pick = (index: number, option: string) =>
    setChoices((current) => ({ ...current, [index]: option }));

  return (
    <div className="msg__form">
      {questions.map((item, index) => (
        <div key={item.question} className="msg__question">
          <div className="msg__question-text">
            <span>{index + 1}.</span> <span>{item.question}</span>
          </div>
          <div className="msg__options">
            {item.options.map((option) => (
              <button
                key={option}
                className={
                  choices[index] === option ? "msg__option msg__option--on" : "msg__option"
                }
                onClick={() => pick(index, option)}
              >
                {option}
              </button>
            ))}
            <button
              className={
                choices[index] === OTHER_OPTION
                  ? "msg__option msg__option--on"
                  : "msg__option"
              }
              onClick={() => pick(index, OTHER_OPTION)}
            >
              {t.aiOtherOption}
            </button>
          </div>
          {choices[index] === OTHER_OPTION && (
            <input
              className="msg__other-input"
              placeholder={t.aiOtherPlaceholder}
              aria-label={t.aiOtherPlaceholder}
              value={custom[index] ?? ""}
              onChange={(event) =>
                setCustom((current) => ({ ...current, [index]: event.target.value }))
              }
            />
          )}
        </div>
      ))}
      <button
        className="btn-solid"
        style={{ padding: 7 }}
        disabled={busy || !complete}
        onClick={() =>
          onSubmit(
            questions
              .map((item, index) => `${item.question} → ${answerFor(index)}`)
              .join("\n"),
          )
        }
      >
        {t.aiAnswerSend}
      </button>
    </div>
  );
}

function ProposalCard({ proposal, studio }: { proposal: ProposalResponse; studio: Studio }) {
  const t = useT();
  const { state, actions } = studio;

  // The server recomputes the authoritative diff and digest; a proposal whose
  // base no longer matches the saved blueprint can never be accepted.
  const stale = state.doc ? proposal.baseDigest !== state.doc.digest : false;
  const invalid = !proposal.exportReady;
  const acceptable = !stale && !invalid;

  const status = stale ? t.stStale : invalid ? t.stInvalid : t.stPending;
  const statusColor = stale ? "var(--yel)" : invalid ? "var(--red)" : "var(--g6)";

  return (
    <div style={{ border: "1px solid var(--ink)", fontSize: 10.5 }}>
      <div
        style={{
          padding: "7px 10px",
          borderBottom: "1px dotted var(--dot)",
          display: "flex",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <b>✻ {t.proposal}</b>
        <b style={{ color: statusColor, fontSize: 9.5 }}>{status}</b>
      </div>

      {proposal.summary && (
        <div style={{ padding: "6px 10px 0", color: "var(--g6)", lineHeight: 1.6 }}>
          {proposal.summary}
        </div>
      )}

      <Diff proposal={proposal} />

      {proposal.diagnostics.length > 0 && (
        <div style={{ padding: "0 10px 8px", fontSize: 9.5, lineHeight: 1.8 }}>
          <div style={{ fontSize: 9, color: "var(--g9)", letterSpacing: "0.08em" }}>
            {t.candidateDiag}
          </div>
          {proposal.diagnostics.map((item, index) => (
            <div key={index}>
              <b style={{ color: markerColor(severityMarker(item.severity)) }}>
                {severityMarker(item.severity)}
              </b>{" "}
              {item.message} · {item.code}
            </div>
          ))}
        </div>
      )}

      {stale && (
        <div
          style={{
            margin: "0 10px 10px",
            border: "2px solid var(--yel)",
            padding: 9,
            fontSize: 9.5,
            lineHeight: 1.6,
          }}
        >
          <b style={{ color: "var(--yel)" }}>{t.staleTitle}</b>
          <br />
          <span style={{ color: "var(--g6)" }}>{t.staleBody}</span>
          <button
            className="btn-solid"
            style={{ marginTop: 8, width: "100%", padding: 7 }}
            onClick={() => void actions.rejectProposal(proposal.id)}
          >
            {t.staleCta}
          </button>
        </div>
      )}

      {invalid && !stale && (
        <div style={{ padding: "0 10px 8px", fontSize: 9.5 }}>
          <b style={{ color: "var(--red)" }}>[xx]</b> {t.invalidNote}
        </div>
      )}

      {!stale && (
        <div style={{ display: "flex", gap: 8, padding: "0 10px 10px" }}>
          <button
            className="btn-outline"
            style={{ flex: 1, textAlign: "center", padding: 7 }}
            onClick={() => void actions.rejectProposal(proposal.id)}
          >
            {t.reject}
          </button>
          <button
            className="btn-solid"
            style={{ flex: 1, textAlign: "center", padding: 7 }}
            disabled={!acceptable}
            onClick={() => void actions.acceptProposal(proposal.id)}
          >
            {t.accept}
          </button>
        </div>
      )}
    </div>
  );
}

export function AiDrawer({ studio, onClose }: { studio: Studio; onClose: () => void }) {
  const t = useT();
  const { state, actions } = studio;
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);

  const send = async (text: string) => {
    const message = text.trim();
    if (!message || thinking) return;
    setInput("");
    setThinking(true);
    await actions.converse(message);
    setThinking(false);
  };

  const approve = async () => {
    if (thinking) return;
    setThinking(true);
    await actions.approvePlan();
    setThinking(false);
  };

  // Proposals created outside the transcript (legacy one-shot flow) still
  // deserve a card; conversational ones render at their turn instead.
  const orphanProposals = state.proposals.filter(
    (proposal) => !state.transcript.some((entry) => entry.proposalId === proposal.id),
  );
  const emptySession =
    state.transcript.length === 0 && orphanProposals.length === 0 && !thinking;

  return (
    <div className="overlay">
      <button className="overlay__scrim" onClick={onClose} aria-label="close" />
      <div className="drawer" role="dialog" aria-label={t.aiAssist}>
        <div className="drawer__head">
          <span>
            <b>✻ {t.aiAssist}</b>{" "}
            {state.proposals.length > 0 && (
              <span style={{ color: "var(--g9)" }}>{t.aiHeadSession}</span>
            )}
          </span>
          <span style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <span style={{ color: "var(--g9)", fontSize: 9.5 }}>
              [ai: {state.authoringAvailable ? t.aiReady : t.aiOff}]
            </span>
            <button onClick={onClose} aria-label="close">
              ✕
            </button>
          </span>
        </div>

        {!state.authoringAvailable ? (
          <div style={{ padding: "16px 14px" }}>
            <div style={{ border: "1px solid var(--ink)", padding: 13 }}>
              <b>{t.aiNoProvider}</b>
              <div
                style={{
                  color: "var(--g6)",
                  lineHeight: 1.7,
                  margin: "8px 0 12px",
                  fontSize: 10,
                }}
              >
                {t.aiNoProviderBody}
              </div>
              <div
                style={{
                  border: "1px dotted var(--b9)",
                  padding: "9px 10px",
                  fontSize: 10,
                  lineHeight: 1.9,
                  color: "var(--g6)",
                }}
              >
                export <b style={{ color: "var(--ink)" }}>LINCH_STUDIO_PROVIDER</b>=anthropic
                <br />
                export <b style={{ color: "var(--ink)" }}>LINCH_STUDIO_MODEL</b>=claude-…
                <br />
                export <b style={{ color: "var(--ink)" }}>LINCH_STUDIO_API_KEY</b>=…
              </div>
              <div
                style={{ color: "var(--g9)", fontSize: 9, marginTop: 9, lineHeight: 1.6 }}
              >
                {t.aiEnvNote}
              </div>
            </div>
          </div>
        ) : (
          <>
            <div className="drawer__body">
              {emptySession && (
                <>
                  <div style={{ color: "var(--g6)", lineHeight: 1.7, fontSize: 10 }}>
                    {t.aiIntro}
                  </div>
                  {EXAMPLES.map((example) => (
                    <button
                      key={example}
                      className="hoverable"
                      style={{
                        border: "1px solid var(--ink)",
                        padding: "9px 11px",
                        lineHeight: 1.5,
                        textAlign: "left",
                      }}
                      onClick={() => void send(example)}
                    >
                      &quot;{example}&quot;
                    </button>
                  ))}
                  <div style={{ borderTop: "1px dotted var(--dot)", paddingTop: 10 }}>
                    <div style={{ fontSize: 9, fontWeight: 700, marginBottom: 6 }}>
                      {t.aiManualOnly}
                    </div>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
                      {MANUAL_ONLY.map((item) => (
                        <span
                          key={item}
                          style={{
                            fontSize: 9,
                            color: "var(--g6)",
                            border: "1px dotted var(--ink)",
                            padding: "2px 7px",
                          }}
                        >
                          {item}
                        </span>
                      ))}
                    </div>
                  </div>
                </>
              )}

              {state.transcript.map((entry, index) => {
                if (entry.role === "user") {
                  return (
                    <div key={index} className="msg msg--user">
                      {entry.content}
                    </div>
                  );
                }
                const isLast = index === state.transcript.length - 1;
                if (entry.kind === "questions") {
                  return (
                    <div key={index} className="msg msg--agent">
                      <Thinking trace={entry.thinking} />
                      <ToolCalls calls={entry.toolCalls} />
                      {entry.note && <div className="msg__note">{entry.note}</div>}
                      {isLast ? (
                        <QuestionForm
                          questions={entry.questions ?? []}
                          busy={thinking}
                          onSubmit={(message) => void send(message)}
                        />
                      ) : (
                        <ol className="msg__questions">
                          {(entry.questions ?? []).map((item) => (
                            <li key={item.question}>
                              <span>{item.question}</span>{" "}
                              <span style={{ color: "var(--g9)" }}>
                                ({item.options.join(" / ")})
                              </span>
                            </li>
                          ))}
                        </ol>
                      )}
                    </div>
                  );
                }
                if (entry.kind === "plan") {
                  return (
                    <div key={index} className="msg msg--agent">
                      <Thinking trace={entry.thinking} />
                      <ToolCalls calls={entry.toolCalls} />
                      {entry.note && <div className="msg__note">{entry.note}</div>}
                      <div className="msg__plan">{entry.plan ?? entry.content}</div>
                      {isLast && (
                        <div className="msg__plan-gate">
                          <button
                            className="btn-solid"
                            style={{ padding: 7 }}
                            disabled={thinking}
                            onClick={() => void approve()}
                          >
                            {t.aiPlanAccept}
                          </button>
                          <span className="msg__plan-hint">{t.aiPlanEditHint}</span>
                        </div>
                      )}
                    </div>
                  );
                }
                const proposal = state.proposals.find((item) => item.id === entry.proposalId);
                return (
                  <div key={index} className="msg msg--agent">
                    <Thinking trace={entry.thinking} />
                    <ToolCalls calls={entry.toolCalls} />
                    {entry.note && <div className="msg__note">{entry.note}</div>}
                    {proposal ? (
                      <ProposalCard proposal={proposal} studio={studio} />
                    ) : (
                      <div style={{ color: "var(--g6)", fontSize: 10 }}>
                        {entry.accepted ? `[ok] ${t.aiAccepted}` : `[--] ${t.aiProposalGone}`}
                      </div>
                    )}
                  </div>
                );
              })}

              {orphanProposals.map((proposal) => (
                <ProposalCard key={proposal.id} proposal={proposal} studio={studio} />
              ))}

              {state.streamingThinking !== null ? (
                <StreamingTrace text={state.streamingThinking} />
              ) : (
                thinking && (
                  <div style={{ color: "var(--g6)", fontSize: 10.5 }}>
                    <span className="blink">▪</span> {t.aiThinking}
                  </div>
                )
              )}
              {state.streamingToolCalls && state.streamingToolCalls.length > 0 && (
                <div className="msg msg--agent">
                  <ToolCalls calls={state.streamingToolCalls} />
                </div>
              )}
            </div>

            <div className="drawer__foot">
              <div
                style={{
                  fontSize: 9,
                  color: "var(--g9)",
                  lineHeight: 1.6,
                  marginBottom: 8,
                }}
              >
                {t.aiRules}
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  style={{ flex: 1, fontSize: 10.5, padding: "8px 10px" }}
                  value={input}
                  placeholder={t.aiPlaceholder}
                  aria-label={t.aiPlaceholder}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void send(input);
                  }}
                />
                <button
                  className="btn-solid"
                  style={{ padding: "8px 14px" }}
                  disabled={thinking || !input.trim()}
                  onClick={() => void send(input)}
                >
                  {t.generate}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

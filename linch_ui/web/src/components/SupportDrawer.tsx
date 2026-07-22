import { useEffect, useRef, useState } from "react";

import type { ProposalResponse, SupportEvidence, SupportRecipe, SupportRecipeFile } from "../api/types";
import type { StreamingSupportToolCall, Support } from "../state/useSupport";
import type { Studio } from "../state/useStudio";

const EXAMPLES = [
  "How do directed workflows share a budget?",
  "Show me how to implement a scheduled multi-agent review with a host cron job.",
  "Build a CI code-review pipeline with security, performance, and style reviewers.",
];

/**
 * Pull a possibly incomplete JSON string value without ever rendering the raw
 * structured-output buffer. The final response still comes from the server's
 * validated SupportTurnResponse; this is just an in-flight preview.
 */
function partialJsonString(raw: string, field: string): string | null {
  const marker = `"${field}"`;
  let fieldStart = raw.indexOf(marker);
  while (fieldStart >= 0) {
    let index = fieldStart + marker.length;
    while (index < raw.length && /\s/.test(raw[index] ?? "")) index += 1;
    if (raw[index] !== ":") {
      fieldStart = raw.indexOf(marker, index);
      continue;
    }
    index += 1;
    while (index < raw.length && /\s/.test(raw[index] ?? "")) index += 1;
    if (raw[index] !== '"') return null;
    index += 1;

    let text = "";
    while (index < raw.length) {
      const char = raw[index] ?? "";
      index += 1;
      if (char === '"') return text;
      if (char !== "\\") {
        text += char;
        continue;
      }
      if (index >= raw.length) return text;
      const escape = raw[index] ?? "";
      index += 1;
      if (escape === "u") {
        const hex = raw.slice(index, index + 4);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) return text;
        text += String.fromCharCode(Number.parseInt(hex, 16));
        index += 4;
        continue;
      }
      if (escape === '"' || escape === "\\" || escape === "/") text += escape;
      else if (escape === "b") text += "\b";
      else if (escape === "f") text += "\f";
      else if (escape === "n") text += "\n";
      else if (escape === "r") text += "\r";
      else if (escape === "t") text += "\t";
      else return text;
    }
    return text;
  }
  return null;
}

export function supportDraftPreview(raw: string): string | null {
  const answer = partialJsonString(raw, "answer");
  if (answer !== null) return answer;
  const title = partialJsonString(raw, "title");
  const overview = partialJsonString(raw, "overview");
  if (title === null && overview === null) return null;
  return [title, overview].filter((item): item is string => item !== null && item.length > 0).join("\n");
}

function StreamingToolCalls({ calls }: { calls: StreamingSupportToolCall[] | null }) {
  if (!calls || calls.length === 0) return null;
  return (
    <div className="msg__tool-calls">
      {calls.map((call) => {
        const line =
          call.status === "running" ? (
            <><span className="blink">▪</span> {call.summary} — reading…</>
          ) : (
            <><b>{call.status === "error" ? "[xx]" : "[ok]"}</b> {call.summary} — {call.resultSummary ?? "completed"}</>
          );
        const className = call.status === "error" ? "msg__tool-call msg__tool-call--error" : "msg__tool-call";
        if (!call.detail) return <div key={call.toolUseId} className={className}>{line}</div>;
        return (
          <details key={call.toolUseId} className={className}>
            <summary>{line}</summary>
            <div className="msg__tool-call-body">{call.detail}</div>
          </details>
        );
      })}
    </div>
  );
}

function StreamingSupportResponse({
  draft,
  toolCalls,
}: {
  draft: string | null;
  toolCalls: StreamingSupportToolCall[] | null;
}) {
  const preview = supportDraftPreview(draft ?? "");
  const label = preview
    ? "Drafting grounded response…"
    : toolCalls && toolCalls.length > 0
      ? "Reading Linch documentation…"
      : "Preparing grounded response…";
  return (
    <div className="msg msg--agent msg--stream" aria-live="polite">
      <div className="msg__stream-label"><span className="blink">▪</span> {label}</div>
      <StreamingToolCalls calls={toolCalls} />
      {preview && <div className="msg__stream-body">{preview}<span className="blink"> ▍</span></div>}
    </div>
  );
}

function Evidence({ evidence, coverage }: { evidence: SupportEvidence[]; coverage: string }) {
  return (
    <details className="msg__thinking" style={{ marginTop: 8 }}>
      <summary>Evidence · {coverage}</summary>
      <div className="msg__thinking-body">
        {evidence.length === 0 ? (
          <span>No supporting corpus section was found.</span>
        ) : (
          evidence.map((item) => (
            <div key={`${item.anchor}:${item.claim}`} style={{ marginBottom: 7 }}>
              <b>{item.claim}</b>
              <div style={{ color: "var(--g9)" }}>{item.anchor}</div>
              {item.excerpt && <div>{item.excerpt}</div>}
            </div>
          ))
        )}
      </div>
    </details>
  );
}

function CodeFile({ file }: { file: SupportRecipeFile }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard?.writeText(file.content);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return (
    <details className="msg__tool-call" open={file.language === "python"}>
      <summary>
        <b>{file.path}</b> · {file.provenance}
      </summary>
      {file.explanation && <div style={{ padding: "6px 10px", color: "var(--g6)" }}>{file.explanation}</div>}
      <div style={{ padding: "0 10px 8px", display: "flex", justifyContent: "space-between" }}>
        <span style={{ color: "var(--g9)" }}>{file.evidence.join(" · ")}</span>
        <button className="link" onClick={() => void copy()}>
          {copied ? "copied" : "copy"}
        </button>
      </div>
      <pre className="msg__thinking-body" style={{ margin: 0, maxHeight: 300, overflow: "auto" }}>
        {file.content}
      </pre>
    </details>
  );
}

function Recipe({ recipe, onPipeline }: { recipe: SupportRecipe; onPipeline: () => void }) {
  return (
    <div style={{ display: "grid", gap: 9 }}>
      <div className="msg__note">
        <b>{recipe.title}</b>
        <br />
        {recipe.overview}
      </div>
      <div style={{ fontSize: 10, color: "var(--g6)" }}>
        <b>Architecture:</b> {recipe.intent.summary}
        {recipe.intent.schedule && <div>Schedule: {recipe.intent.schedule}</div>}
        {recipe.intent.workflowShape && <div>Workflow: {recipe.intent.workflowShape}</div>}
      </div>
      <div>
        <b style={{ fontSize: 10 }}>FILES</b>
        {recipe.files.map((file) => <CodeFile key={file.path} file={file} />)}
      </div>
      {recipe.tests.length > 0 && (
        <div>
          <b style={{ fontSize: 10 }}>OFFLINE TESTS</b>
          {recipe.tests.map((file) => <CodeFile key={file.path} file={file} />)}
        </div>
      )}
      <div className="msg__note" style={{ fontSize: 10 }}>
        <b>Start here</b>
        <ol style={{ margin: "5px 0 0", paddingLeft: 17 }}>
          {recipe.handoff.startHere.map((item) => <li key={item}>{item}</li>)}
        </ol>
        {recipe.handoff.environment.length > 0 && <div>Environment: {recipe.handoff.environment.join(", ")}</div>}
        {recipe.handoff.commands.map((item) => (
          <div key={item.command}><code>{item.command}</code> — {item.purpose}</div>
        ))}
        {recipe.handoff.todos.map((item) => (
          <div key={item.description}>[{item.blocking ? "TODO" : "note"}] {item.description}</div>
        ))}
      </div>
      <button className="btn-outline" onClick={onPipeline}>Turn into pipeline</button>
    </div>
  );
}

const SAFE_DRAFT_CODES = new Set([
  "semantic.provider_required",
  "semantic.provider_model_required",
  "semantic.headless_limit_required",
  "semantic.headless_permissions",
  "semantic.webhook_signing_secret_required",
]);

function Proposal({ proposal, studio, projectId }: { proposal: ProposalResponse; studio: Studio; projectId?: string | null }) {
  const { actions } = studio;
  const hardErrors = proposal.diagnostics.filter(
    (item) => item.severity === "error" && !SAFE_DRAFT_CODES.has(item.code),
  );
  const acceptAsDraft = !proposal.exportReady && hardErrors.length === 0;
  return (
    <div className="msg__note">
      <b>Pipeline proposal</b>
      <div>{proposal.summary ?? "Review the semantic diff in Studio before accepting."}</div>
      {!projectId ? (
        <div style={{ color: "var(--red)" }}>Open the target project to review this proposal.</div>
      ) : (
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <button className="btn-outline" onClick={() => void actions.rejectProposal(proposal.id)}>Reject</button>
          <button className="btn-solid" disabled={!proposal.exportReady && !acceptAsDraft} onClick={() => void actions.acceptProposal(proposal.id)}>
            {acceptAsDraft ? "Accept as draft" : "Accept"}
          </button>
        </div>
      )}
      {acceptAsDraft && <div style={{ color: "var(--yel)", marginTop: 7 }}>Export remains blocked until the listed configuration gaps are resolved.</div>}
    </div>
  );
}

export function SupportDrawer({
  support,
  studio,
  projectId,
  onClose,
}: {
  support: Support;
  studio: Studio;
  projectId?: string | null;
  onClose: () => void;
}) {
  const { state, actions } = support;
  const [input, setInput] = useState("");
  const bodyRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const body = bodyRef.current;
    if (body) body.scrollTop = body.scrollHeight;
  }, [state.error, state.responseDraft, state.streamingToolCalls, state.transcript]);
  const send = async () => {
    const text = input.trim();
    if (!text) return;
    setInput("");
    await actions.run(text, { projectId });
  };

  return (
    <div className="overlay">
      <button className="overlay__scrim" onClick={onClose} aria-label="close" />
      <div className="drawer" role="dialog" aria-label="Linch support">
        <div className="drawer__head">
          <span><b>✻ Support</b> <span style={{ color: "var(--g9)" }}>docs · implementation · pipelines</span></span>
          <span style={{ display: "flex", gap: 9 }}>
            <button className="link" onClick={actions.clear}>clear</button>
            <button onClick={onClose} aria-label="close">✕</button>
          </span>
        </div>
        <div className="drawer__body" ref={bodyRef}>
          {state.transcript.length === 0 && (
            <>
              <div style={{ color: "var(--g6)", lineHeight: 1.7, fontSize: 10 }}>
                Ask about Linch docs, request a static implementation recipe, or describe a pipeline.
                Recipes are never executed; pipeline work needs a confirmation and an open project.
              </div>
              {EXAMPLES.map((example) => (
                <button key={example} className="hoverable" style={{ border: "1px solid var(--ink)", padding: "9px 11px", textAlign: "left" }} onClick={() => { setInput(example); }}>
                  “{example}”
                </button>
              ))}
            </>
          )}
          {state.transcript.map((entry, index) => {
            if (entry.role === "user") return <div key={index} className="msg msg--user">{entry.content}</div>;
            const response = entry.response;
            if (!response) return <div key={index} className="msg msg--agent">{entry.content}</div>;
            return (
              <div key={index} className="msg msg--agent">
                {response.kind === "answer" && <div style={{ whiteSpace: "pre-wrap" }}>{response.answer}</div>}
                {response.kind === "recipe" && response.recipe && <Recipe recipe={response.recipe} onPipeline={() => void actions.turnRecipeIntoPipeline(projectId)} />}
                {response.kind === "mode_confirmation" && (
                  <>
                    <div>{response.answer}</div>
                    {response.pipelineIntent && <div className="msg__note">{response.pipelineIntent.proposedComponents.join(" · ")}</div>}
                    <button className="btn-solid" disabled={state.busy || !projectId} onClick={() => void actions.confirmPipeline(projectId)}>
                      {projectId ? "Confirm pipeline authoring" : "Open a project first"}
                    </button>
                  </>
                )}
                {response.kind === "questions" && (
                  <PipelineQuestions questions={response.questions} busy={state.busy} onSend={(value) => void actions.answerPipelineQuestions(value, projectId)} />
                )}
                {response.kind === "plan" && (
                  <>
                    <div className="msg__plan">{response.plan}</div>
                    <button className="btn-solid" disabled={state.busy} onClick={() => void actions.approvePipelinePlan(projectId)}>Approve plan</button>
                  </>
                )}
                {response.kind === "proposal" && response.proposal && <Proposal proposal={response.proposal} studio={studio} projectId={projectId} />}
                {response.followUp && <div className="msg__note">{response.followUp}</div>}
                <Evidence evidence={response.evidence} coverage={response.coverage} />
              </div>
            );
          })}
          {state.busy && (
            <StreamingSupportResponse
              draft={state.responseDraft}
              toolCalls={state.streamingToolCalls}
            />
          )}
          {state.error && <div className="msg msg--agent" style={{ color: "var(--red)" }}>[xx] {state.error}</div>}
        </div>
        <div className="drawer__foot" style={{ display: "flex", gap: 8 }}>
          <input
            value={input}
            disabled={state.busy || state.loading}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Enter") void send(); }}
            placeholder={state.available ? "Ask Linch Support…" : "Ask about a deterministic pipeline, or configure LINCH_STUDIO_PROVIDER for docs"}
            style={{ flex: 1 }}
          />
          <button className="btn-solid" disabled={state.busy || state.loading || !input.trim()} onClick={() => void send()}>
            {state.busy ? "Working…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}

function PipelineQuestions({
  questions,
  busy,
  onSend,
}: {
  questions: Array<{ question: string; options: string[] }>;
  busy: boolean;
  onSend: (value: string) => void;
}) {
  const [answer, setAnswer] = useState("");
  return (
    <div>
      {questions.map((item) => (
        <div key={item.question} className="msg__note">
          <b>{item.question}</b><br />
          {item.options.join(" / ")}
        </div>
      ))}
      <textarea value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="Your choices and constraints" style={{ width: "100%", minHeight: 70 }} />
      <button className="btn-solid" disabled={busy || !answer.trim()} onClick={() => onSend(answer)}>Continue</button>
    </div>
  );
}

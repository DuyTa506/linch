# Changelog

Notable changes to `linch-rust`: the Rust crates under `crates/` and the
`linch-rust` Python client that ships the `linch-server` binary. The Python
package `linch` has its own [changelog](../CHANGELOG.md).

## Unreleased

### Added

- Twelve observe-only hook points, for parity with Python's lifecycle hooks:
  `agent_start`, `agent_stop`, `turn_start`, `provider_call_start`,
  `provider_call_stop`, `tool_use_start`, `tool_use_stop`,
  `post_tool_use_failure`, `pre_compact`, `post_compact`, `subagent_start`,
  `subagent_stop`. Each answers `()` (`{}` on the wire), can never fail
  closed, and costs nothing when no hook is registered on it. Available to
  `@callback` in `linch_rust` and to `Hook<P>` implementations in
  `linch-core`.
- `before_provider_call`: a hook point for the full provider request about to
  be sent. A hook may rewrite it (the rewrite is what is sent and what
  `context_build` records) or block the call, ending the run before the
  provider is ever reached. Defaults open, but unlike the observe-only
  points its blocking answer can fail closed. Available to `@callback` in
  `linch_rust` and to `Hook<BeforeProviderCall>` in `linch-core`.
- `after_provider_call`: a hook point for the response a provider call just
  assembled, before it is staged and appended to the session. A hook may
  rewrite it (the rewrite is what gets staged and appended) or block it,
  ending the run before anything is staged. Defaults open, but like
  `before_provider_call` its blocking answer can fail closed. Available to
  `@callback` in `linch_rust` and to `Hook<AfterProviderCall>` in
  `linch-core`.
- `stop` can now rewrite the run's final-answer text, or block the stop
  outright. This is Python's `before_final_answer` hook point, folded into
  `stop` rather than added as a separate point: the rewrite replaces only the
  text the run reports as its result, not the assistant message already
  appended to the session (that message is logged before `stop` ever runs).
  Available as `{"action": "rewrite", "text"}` / `{"action": "block",
  "reason"}` to `@callback` in `linch_rust` and as `StopResponse::Rewrite`/
  `StopResponse::Block` to `Hook<Stop>` in `linch-core`.
- `event_emit`: a thirteenth observe-only hook point, for parity with
  Python's `EVENT_EMIT`. It fires once per event, in delivery order, but
  only for an event that is both streamed to the caller and durably logged:
  never for a hook's own record (it would otherwise recurse into itself)
  and never for a stream-only delta or tool-progress event. Answers `()`
  (`{}` on the wire) and can never fail closed. Available to `@callback` in
  `linch_rust` and to `Hook<EventEmit>` in `linch-core`.
- `after_provider_call` can retry, as Python's `retry`/`force_continue` there: the response is kept and logged, none of its tool calls run, and the feedback goes to the model (in a `<system-reminder>`, or as the first call's error result) before another turn; `{"action": "retry"|"force_continue", "feedback"}` on the wire, `AfterProviderCallResponse::Retry` in `linch-core`.
- `stop` can retry the final answer once per run call, as Python's `before_final_answer` `retry` (`_MAX_FINAL_ANSWER_REENTRIES`); a spent retry counts as `allow`. `{"action": "retry", "feedback"}` on the wire, `StopResponse::Retry` in `linch-core`.
- `stop` accepts `{"action": "force_continue", "feedback"}`, Python's name for its `continue`.
- `Phase::AssistantReceived` records an `after_provider_call` retry in an optional `retry_feedback`, durable with the paid response; checkpoints without one are unchanged, but a binary older than this one cannot resume a run whose checkpoint has one.
- `spec.provider_retry` retries a provider call that failed with a retryable error (no connection or no response, 408, 429, 5xx) on the same model, after an exponential, jittered wait that honours `Retry-After`: `{"max_attempts"?, "base_delay_ms"?, "max_delay_ms"?, "jitter"?}`, on by default with Python's `RetryOptions` (5, 1000, 30000, 0.2); `{"max_attempts": 1}` turns it off. The default stays off the wire, so a spec that does not set it keeps its run contract. Each attempt is its own durable delivery; a crash during the wait resumes with the next attempt and keeps the turn's count. `ProviderRetry` in `linch-core` and the Python client.
- A run that retried or failed over holds `provider_retry` / `model_fallback` events, which a binary older than this one refuses as an unknown event type: it cannot read or resume such a run (the error is explicit; nothing is resumed wrongly). Since retries are on by default, any run that met a retryable failure is one, so roll back only once those runs have ended.
- `ProviderError::Connect`: a connection never made (refused, DNS, TLS handshake, connect timeout), so the request never went out; its error event's `kind` is `connect`, and a scripted turn can fail with `{"type": "connect", "message"}`.
- A call that failed before costing anything (`Connect`, or an error status before any stream event) settles with a zero usage instead of an unknown one: no `usage` event, `usage_complete` stays true, and a capped budget may retry it. A reset, a read timeout or a failure after output began stays unknown and, under a cap, still ends the run.
- `spec.fallback_models` fails a run over to the next model, in order, once a model has no retry left; the run keeps that model, a resumed run included, and its usage and cost name it. Empty by default.
- `provider_retry` and `model_fallback` events report each retry (`model`, `retry`, `delay_ms`, `error`) and each failover (`from_model`, `to_model`, `error`); the Python client decodes them as `ProviderRetryEvent` and `ModelFallbackEvent`.
- Structured output: `spec.output_schema` (`{name, schema, strict?, description?}`) makes the final answer a JSON object checked against the schema (raw or in a fenced block); the parsed value is `result.structured_output`. `OutputSchema` in `linch_rust` and `linch-core`.
- `spec.structured_output_retries` (default 0) sends a failing answer back to the model with Python's feedback wording, recorded as a `verification` event (`verifier: "output_schema"`, `action: "retry"`); the count is read back on resume, so a crash never grants more.
- Past `structured_output_retries` the run fails: `verification` with `action: "stop"`, an `error` named `StructuredOutputError`, and a `result` of subtype `error` with `structured_error`.
- `stop` runs only on an answer that satisfies the schema and gets the parsed `structured_output` in its request; a `rewrite` is checked again and one that fails the schema fails the run.
- `Agent.run(output_schema=...)`, `run.start` `output_schema` and `RunOptions::output_schema` give one run its own schema; it is stored on the run's first `init` (`InitEvent.output_schema`) so a resume or approve keeps it.
- `InvalidOutputSchema` (code -32031, `RunError::InvalidOutputSchema`): a run's own schema does not compile or its root is not an object; nothing is written.
- `InitEvent.output_schema`: only the first `init` of a run started with its own schema has it; other events are unchanged, but a binary older than this one cannot read the events of a run whose `init` has one.
- Anthropic, OpenAI Chat, OpenAI Responses and Gemini send the output schema in their native structured-output form (`output_config.format`, `response_format`, `text.format`, `responseMimeType` with `responseJsonSchema`); no forced final tool. The shapes follow each vendor's API docs and have not been run against a live API. A fallback model (`spec.fallback_models`) is sent the same schema and its answers are checked the same way.
- `AskUser` (config `ask_user: true`, off by default, as Python's opt-in `AskUserPlugin`): 1 to 4 questions with 2 to 4 options each; a call suspends the run with an `input_request` event (its `questions`) and a `suspended` event of status `awaiting_input`. A subagent's call gets an error instead.
- `Agent.answer(run_id, {tool_use_id: Answer(...)})`, `run.answer` and `Agent::answer` commit each answer as its call's result and continue the run; a request left out is asked again, `Answer.declined(note)` declines. The answer is durable before the run goes on, so a crash never asks twice or answers twice.
- `InvalidAnswer` (code -32032, `RunError::InvalidAnswer`): an answer that names no pending request, misses a question or answers a single-select question with a list; nothing is written.
- `Phase::AwaitingInput` and `InputRequestEvent.questions` are new in the journal; other checkpoints and events are unchanged, but a binary older than this one cannot resume a run waiting for input.
- `Bash` takes `run_in_background`: the command runs on after the call returns its `process_id`; `BashOutput` (read scope) returns what it printed since the last read, waiting up to `wait_ms` for it to end, and `BashKill` (exec scope) kills its process group. At most 4 run at once per session; each is killed after `timeout_ms` (30 minutes by default), when its session closes, when its agent closes and when the server dies, even by SIGKILL.
- A background process does not survive a server restart: `BashOutput` and `BashKill` on an id from before it fail with `no_such_process`, saying so.

### Changed

- `ProviderError::retryable()` is true for an HTTP 408, as Python treats a request timeout.
- With an `after_provider_call` hook attached, an accepted call's receipt and `usage` event are written before the hooks run (`provider_call_stop` included), with `ProviderPending` at the totals that count them; the response is written after them, as before. A crash while such a hook runs no longer loses the paid usage: the resume keeps it in the totals and the budget and calls again under the next delivery attempt, which spends no `provider_retry`. The run costs one more write per accepted call; without an `after_provider_call` hook nothing changes, a `provider_call_stop` observer alone included. `ReconcileError::MissingAssistant` is gone.
- A binary older than this one refuses to resume a run that crashed between such a receipt and its response (an accepted receipt with no `assistant` event, under `provider_pending`): it fails with `MissingAssistant` and resumes nothing wrongly, but an older `linch-worker` marks such a run `failed` (it fails a run on any reconcile error). Roll back only once those runs have been resumed or ended.
- A call that failed with a 500, 502 or 504 before any stream event now settles with an unknown usage instead of a zero one: a gateway can send those after the upstream model already ran and billed. Only a connection never made, a 4xx (408 and 429 included), a 503 or a 529 before any event counts as costing nothing; any other status is unknown too. Under a capped `budget` such a failure is no longer retried, and `usage_complete` is false.

- `stop` feedback (`continue`) reaches the model wrapped in `<system-reminder>`, as Python's does, instead of as bare text.
- A workspace agent now also has `BashOutput` and `BashKill` (`bash: false` leaves all three out), so its run contract changes: a run started under an older binary cannot be resumed.
- `agent.close_session` and `session.close` also kill the session's background `Bash` commands.
- DeepSeek and the vLLM, SGLang and llama.cpp profiles still refuse an output schema, now saying why: no native JSON-schema response format that carries the schema.

- An intercept callback without `timeout_ms` (Python `timeout`) no longer times out
  after 30 seconds: it waits until the client answers, the run is cancelled or the
  connection ends, as Python's hooks do. Set `timeout` to bound it.
- `@tool` and `@callback` accept plain functions as well as `async` ones, as Python's tools and
  hooks do; a plain function runs in a worker thread.
- `Edit` accepts an empty `old_string`, as Python's does, instead of refusing it as invalid input: an empty file takes `new_string`; a non-empty file is refused as ambiguous unless `replace_all` is set, which inserts at every position and needs the whole file read.
- `Edit` and `Write` on a file changed since it was read now say `Read this file again before editing it.` / `Read this file again before writing it.` (were `<path> changed since it was last read; Read it again.`), and `Read` of a file that changed mid-read says `File changed while reading; Read this file again.`, the words Python's tools use. The error codes are unchanged.

### Upgrading

- Runs of workspace agents started before this release cannot be resumed: the workspace tool list gained `BashOutput` and `BashKill`, so the run contract changed and a resume ends with `ContractMismatch`. Let those runs finish on the old binary, or start new runs.

## 0.1.1 - 2026-11-07

### Fixed

- The task tools (`TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate`) now belong
  to the session of the root run of the agent tree. A subagent's worker used to
  see an empty list and could not update the coordinator's tasks; it now reads
  and updates them. Unrelated sessions stay apart.
- `TaskStop` on a worker that already finished no longer says it stopped it. The
  reply says the worker already finished and how it ended (`completed`, `failed`
  or `killed`); the worker's status is unchanged, as before.

### Changed

- `linch_tools::task_tools` takes the run store as a second argument:
  `task_tools(store, runs, namespace)`. `TaskCreate::new`, `TaskList::new`,
  `TaskGet::new` and `TaskUpdate::new` take it too, as `new(store, runs,
  namespace)`. Hosts that build the tools themselves pass their `RunStore`; a
  call whose run the store does not know uses its own session.

### Tests

- New tests pin that none of the Python bugs #99 to #108 is present in the Rust
  port (tool scope, file namespaces, secrets kept out of argv, PreToolUse
  actions, Grep cancellation, offload, the roster, worker status and the child
  tool filter).

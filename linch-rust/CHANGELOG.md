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
- `spec.provider_retry` retries a provider call that failed with a retryable error (no response, 408, 429, 5xx) on the same model, after an exponential, jittered wait that honours `Retry-After`: `{"max_attempts"?, "base_delay_ms"?, "max_delay_ms"?, "jitter"?}`, `{}` for Python's `RetryOptions` defaults (5, 1000, 30000, 0.2). Off when absent. Each attempt is its own durable delivery; a crash during the wait resumes with the next attempt and keeps the turn's count. `ProviderRetry` in `linch-core` and the Python client.
- `spec.fallback_models` fails a run over to the next model, in order, once a model has no retry left; the run keeps that model, a resumed run included, and its usage and cost name it. Empty by default.
- `provider_retry` and `model_fallback` events report each retry (`model`, `retry`, `delay_ms`, `error`) and each failover (`from_model`, `to_model`, `error`); the Python client decodes them as `ProviderRetryEvent` and `ModelFallbackEvent`.

### Changed

- `ProviderError::retryable()` is true for an HTTP 408, as Python treats a request timeout.

- `stop` feedback (`continue`) reaches the model wrapped in `<system-reminder>`, as Python's does, instead of as bare text.

- An intercept callback without `timeout_ms` (Python `timeout`) no longer times out
  after 30 seconds: it waits until the client answers, the run is cancelled or the
  connection ends, as Python's hooks do. Set `timeout` to bound it.
- `@tool` and `@callback` accept plain functions as well as `async` ones, as Python's tools and
  hooks do; a plain function runs in a worker thread.

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

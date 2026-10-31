# Module Inventory

> Part of the [Linch architecture guide](./README.md).

This inventory follows the current `src/linch` tree (the contributor guide
contains the complete tracked `git ls-files src tests` listing). Names in the
top-level package are public only when they are in `linch.__all__`; submodule
paths and underscore-prefixed names are implementation details unless a page
explicitly documents them as an adapter seam. Durable memory is file-backed
through `context/file_memory.py`, and OpenTelemetry instrumentation is kept in
the private `_telemetry.py` adapter.

## Kernel and run loop

| Path | Responsibility |
|---|---|
| `__init__.py` | Lazy top-level API. It resolves the names in `linch.__all__` without importing optional provider, MCP, Postgres, or worker integrations eagerly. |
| `agent.py` | `Agent` configuration, per-agent `Context`, registries, session lifecycle, provider preparation, permissions, hooks, filesystem setup, and default `ReliabilityPolicy()`. |
| `session.py` | `Session`, `RunOptions`, session lifecycle, run/resume admission, append-only history access, per-run dependencies, principals, and background work. |
| `session_log.py` | Append-only `SessionLog`; `provider_view` and `full_history` projections; logged compaction projections and history fingerprints. |
| `profiles.py` | Immutable, versioned `Profile` composition, plugin rows, tool restrictions, output schemas, budgets, and resume content hashes. |
| `lifecycle.py` | Typed lifecycle-point descriptors and dispatch modes used by the event catalog and kernel bus. |
| `kernel/` | Dependency-free `Context`, reversible `Disposable` effects, `EffectScope`, and `EventBus`. See [kernel.md](./kernel.md). |
| `loop/runner.py` | `run_loop()` and `resume_loop()` entry points plus turn choreography and terminal cleanup. |
| `loop/runtime.py` | Run-scoped checkpoint, ledger, lifecycle, hook, telemetry, and lease-loss state used by the runner. |
| `loop/request.py` | User-message and `ProviderRequest` construction, context-builder output merge, and provider capability downgrades. |
| `loop/provider_turn.py` | Provider-turn preparation, streaming handoff, accepted assistant persistence, and response-hook dispatch. |
| `loop/streaming.py` | Normalized provider stream parsing and context-length/model-fallback recovery. |
| `loop/tool_batch.py` | Tool-batch checkpoints, event projection, result-message append, recovery, and loop-guard evaluation. |
| `loop/checkpoint.py` | Current wire-format checkpoint/event serialization, event buffering, model-call recovery cursors, and extension state. |
| `loop/terminals.py` | Terminal result/event tails, structured-output validation, and completion gates. |
| `loop/finalize.py` | Final text/final-tool finalization and `TurnStopping` dispatch before terminal-tool acknowledgement. |
| `loop/turn_stopping.py` | `TurnStopping`, `TurnStoppingDecision`, `TurnStoppingDispatcher`, and the serial veto/continue/fail seam. |
| `loop/feedback.py` | Durable feedback messages for rejected provider output, final-answer checks, and turn continuation. |
| `loop/reliability.py` | Run-scoped reliability state and prerequisite hook implementation. |
| `loop/contracts.py` | Resume-safe run-contract construction, including principal and reliability configuration. |
| `loop/resume.py` | Checkpoint recovery and replay of pending provider/tool work. |
| `loop/startup.py` | Durable startup phase and user-prompt append handling. |
| `loop/dispatch.py` | Hook lifecycle dispatch adapters. |
| `loop/__init__.py` | Public loop re-exports: `run_loop`, `resume_loop`, request helpers, and provider capability application. |

## Core support modules

| Path | Responsibility |
|---|---|
| `_blocking.py` | Bounded daemon-thread offload for synchronous host I/O. |
| `_client_lifecycle.py` | Async client close/ownership helpers used by provider adapters. |
| `_http_errors.py` | Shared HTTP error normalization for provider clients. |
| `_json.py` | Strict JSON-safe traversal and normalization helpers. |
| `_prompt_cache.py` | Prompt-cache advisory and prefix identity helpers. |
| `_telemetry.py` | Private OpenTelemetry source instrumentation for runs, turns, provider calls, tools, compaction, and store metrics. |
| `_token_estimation.py` | Provider-independent token estimation policies. |
| `_tracing.py` | Trace-context and `trace_id` propagation helpers. |
| `_version.py` | Package version metadata. |
| `_wire.py` | Current persisted wire-generation enforcement. |
| `abort.py` | Cooperative abort signal and abort errors. |
| `config.py` | `FeatureFlags` and system-prompt configuration. |
| `durability.py` | Durable inbox options and notification delivery envelopes. |
| `errors.py` | Public Linch error hierarchy and typed run/provider/tool failures. |
| `events.py` | Typed stream events and strict current-wire serialization. |
| `middleware.py` | Tool middleware context and input/result records. |
| `recovery.py` | Optional output-truncation recovery policy. |
| `reliability.py` | Public response-reliability policy, prerequisites, and completion gates. |
| `scheduler.py` | Canonical tool resolution, authorization ordering, resource-aware batches, retries, timeouts, and result offload. |
| `types.py` | Shared messages, blocks, provider requests, usage, schemas, and stop reasons. |
| `verification.py` | Final-answer verifier protocols and verdict evaluation. |

## Providers and model accounting

| Path | Responsibility |
|---|---|
| `providers/base.py` | `BaseProvider`, `ProviderCapabilities`, thinking configuration, and normalized provider protocol. |
| `providers/openai_chat.py` | OpenAI Chat Completions and OpenAI-compatible endpoints. |
| `providers/openai_responses.py` | OpenAI Responses API, including stateful response ids and native reasoning options. |
| `providers/anthropic.py` | Anthropic streaming, prompt caching, and thinking options. |
| `providers/gemini.py` | Gemini/Vertex AI adapter using the optional `google-genai` SDK; SDK objects stay inside the adapter. |
| `providers/deepseek.py` | DeepSeek-native options and reasoning-content handling. |
| `providers/llamacpp.py` | llama.cpp OpenAI-compatible server adapter and context-window preparation. |
| `providers/vllm.py` | vLLM OpenAI-compatible server adapter. |
| `providers/sglang.py` | SGLang OpenAI-compatible server adapter. |
| `providers/catalog.py` | Provider model metadata and model listing helpers. |
| `providers/limiter.py` | Optional provider concurrency gate. |
| `providers/retry.py` | Provider/tool retry options and retry wrapper. |
| `model_calls.py` | Durable model-call intents, receipts, projections, and cumulative usage accounting. |
| `openai_responses.py` | `OpenAIOptions`, `OpenAIReasoning`, Responses wire translation, and low-level streaming helpers used by the provider adapter. |
| `compaction.py` | Context compaction strategies, compaction accounting, and provider-backed summaries. |
| `budget.py` | Shared per-run and subagent token/cost budgets. |
| `pricing.py` | Known model pricing and cost calculation. |

## Durable state and scale-out

| Path | Responsibility |
|---|---|
| `run_store.py` | `RunStore`, optional `RunLeaseStore`, event-batch and model-input snapshot protocols; `RunCheckpoint`, `RunRecord`, `RunLease`, run contracts, wire codecs, and `InMemoryRunStore`. |
| `run_stores/__init__.py` | Explicit durable run-store namespace and Postgres export boundary. |
| `run_stores/postgres.py` | `PostgresRunStore` with fenced writes, lease epochs, checkpoints, model-input snapshots, and `(run_id, seq)` event rows. |
| `sessions/store.py` | `SessionStore`, `ProviderViewSnapshotStore`, `SessionInboxStore`, and session record/message types. |
| `sessions/memory.py` | `InMemorySessionStore` for process-local tests and development. |
| `sessions/postgres.py` | `PostgresSessionStore` for durable session messages, task rows, provider-view snapshots, and inbox delivery. Import it from `linch.sessions.postgres`. |
| `storage/` | Internal shared asyncpg pool/schema lifecycle in `storage/_pg.py`; this package is not part of the public API. |
| `worker/__init__.py` | Explicit optional worker namespace; core `import linch` does not load PgQueuer. |
| `worker/dispatch.py` | Atomic pending-run creation and PgQueuer job enqueueing. |
| `worker/runtime.py` | `Worker` fleet runtime, run lease heartbeat, takeover, drain, and retry handling. |
| `worker/schedules.py` | UTC cron/interval schedule definitions and deferred schedule jobs. |
| `worker/tools.py` | Session-bound schedule tools mounted by the host. |

The worker uses `PostgresRunStore.claim()`/`renew()`/`release()` with a
monotonic fencing epoch. It rebuilds PgQueuer managers after transient
database/listener failures, retries infrastructure failures with
`DatabaseRetryEntrypointExecutor`, and lets stale queue jobs be reclaimed by
PgQueuer. The worker acknowledges a job only after a terminal run state is
durable.

## Tools, policy, and context

| Path | Responsibility |
|---|---|
| `tools/base.py` | Duck-typed tool protocol, `ToolContext`, `ToolResult`, output contracts, scopes, resource accesses, citations, and stable idempotency keys. |
| `tools/function.py` | `FunctionTool` and `tool()` adapter; synchronous functions run in bounded blocking workers. |
| `tools/registry.py` | `ToolRegistry`, reversible registration, `empty_tools()`, and `workspace_tools()`. |
| `tools/search.py` | `ToolSearchPlugin` request-catalog deferral, keyword/select search, loaded-tool persistence, and deferred-call guards. |
| `tools/pipeline.py` | Open `tools/pre-execute` → `execute` → `post-execute` pipeline. |
| `tools/builtin.py` | Workspace read/write/shell tools. |
| `tools/tasks.py` | Session task tools. |
| `tools/ask_user.py` | Host-provided user-question tool. |
| `tools/skill.py` | Skill invocation tool. |
| `tools/subagent.py` | Subagent spawning and input-aware parallel eligibility. |
| `tools/subagent_continue.py` | Continue a retained child session by worker id or display name. |
| `tools/subagent_stop.py` | Stop a background worker while retaining its handle for later continuation. |
| `tools/_worker_utils.py` | Shared worker id/display-name resolution. |
| `tools/isolation.py` | Per-subagent isolation backend and temporary directory support. |
| `tools/wrappers/` | Optional timeout and metrics execution wrappers. |
| `permissions/engine.py` | Canonical-input permission decisions, approval callbacks, and durable same-turn decision handling. |
| `permissions/rules.py` | `ToolRule`, `PathRule`, and `BashRule`, including role conditions. |
| `permissions/ruleset.py` | Layered permission rule sets. |
| `permissions/principal.py` | Immutable `Principal(tenant, user, roles)` identity used by one run and its tool calls. |
| `context/builder.py` | Async context-builder protocol, context budgets, builder chains, and ephemeral request context. |
| `context/file_memory.py` | `FileMemoryContextBuilder`, which caches `/memories/MEMORY.md` for one run and injects at most 200 lines. |
| `rag/__init__.py` | Request-scoped retrieval protocol (`RagConnector`, `RetrievedChunk`), sourced context injection, checkpointed retrieval state, and optional citation grounding on `TurnStopping`. |
| `filesystem/backend.py` | Virtual `FileBackend`, path normalization, scoped backends, in-memory state, and composite routing. |
| `filesystem/disk.py` | Disk-backed virtual files. |
| `filesystem/postgres.py` | `PostgresFileBackend` for durable virtual files; import it from `linch.filesystem.postgres`. |
| `filesystem/offload.py` | Threshold-based large-result offload to a virtual filesystem. |
| `filesystem/tools.py` | `ls`, `read_file`, `write_file`, and `edit_file` virtual filesystem tools. |
| `hooks/` | Typed hook contexts, lifecycle dispatcher, context injection, tool middleware, redaction, read-before-write, caching, verification, and stop hooks. |
| `skills/` | `SKILL.md` loading, listing, substitution, overlays, built-in skills, and system reminders. |
| `subagents/` | Named agent definitions, child-agent construction, in-process worker handles, and subagent runners. |
| `coordination/__init__.py` | In-process coordination exports; the `MailboxPlugin` is defined in `coordination/mailbox/__init__.py`. |
| `coordination/mailbox/__init__.py` | Mailbox protocol and in-memory implementation exports. |
| `coordination/mailbox/core.py` | Mailbox messages, claims, leases, and in-memory mailbox delivery. |
| `coordination/mailbox/correlation.py` | Correlator handshakes for request/response peer messages. |
| `coordination/_claim_validation.py` | Shared claim-token validation. |
| `coordination/send_message.py` | Peer-message tool implementation. Durable scheduling and PgQueuer dispatch are in `worker/`. |
| `mcp/` | Optional MCP server configuration, client connection, naming, permission bridge, and tool adapters. |

The package-level paths below are the smaller protocol and adapter modules that
support those rows:

| Path | Responsibility |
|---|---|
| `context/__init__.py` | Context-builder and file-memory exports. |
| `execution/__init__.py` | Unified execution capability exports. |
| `execution/backend.py` | `ExecutionBackend`, shell/filesystem transport protocols, and security metadata. |
| `execution/local.py` | Host-local shell/filesystem execution backend. |
| `execution/remote.py` | Remote execution backend with resumable policy identity. |
| `filesystem/__init__.py` | Filesystem protocol, backend, and tool exports. |
| `hooks/__init__.py` | Hook dispatcher, contexts, adapters, and hook type exports. |
| `hooks/adapters.py` | Built-in adapters for context, middleware, verification, and stop boundaries. |
| `hooks/contexts.py` | Typed hook context payloads. |
| `hooks/dispatcher.py` | Ordered hook dispatch and failure isolation. |
| `hooks/read_before_write.py` | Optional read-before-write guard. |
| `hooks/redaction.py` | Tool/event redaction hook. |
| `hooks/tool_cache.py` | Optional read-tool result cache hook. |
| `hooks/types.py` | Hook protocols and `HookResult`. |
| `kernel/__init__.py` | Kernel public exports. |
| `kernel/context.py` | Scoped service registry and plugin activation context. |
| `kernel/effects.py` | Reversible `Disposable` and effect scopes. |
| `kernel/errors.py` | Kernel configuration and lifecycle errors. |
| `kernel/events.py` | Typed event bus and lifecycle dispatch modes. |
| `kernel/plugins.py` | Plugin protocol, dependency activation, scope audit, and mount disposal. |
| `loop_guard/__init__.py` | Loop-guard protocol and decision exports. |
| `loop_guard/guard.py` | Repeated-call, failure-streak, and max-turn detection. |
| `mcp/__init__.py` | Optional MCP adapter exports. |
| `mcp/client.py` | MCP client session and transport lifecycle. |
| `mcp/config.py` | MCP server configuration. |
| `mcp/naming.py` | Stable MCP tool naming. |
| `mcp/permission_bridge.py` | MCP metadata to Linch permission mapping. |
| `mcp/result.py` | MCP result normalization. |
| `mcp/tool.py` | MCP tool adapter. |
| `permissions/__init__.py` | Permission engine, rule, ruleset, and principal exports. |
| `permissions/keys.py` | Canonical permission-key construction. |
| `sessions/__init__.py` | Session-store and session helper exports. |
| `sessions/fork.py` | Validated portable session forks. |
| `sessions/tasks.py` | Session task rows and task-id allocation. |
| `skills/__init__.py` | Skill loading, listing, and type exports. |
| `subagents/__init__.py` | Child-agent and worker exports. |
| `testing/__init__.py` | Reusable contract-test exports. |
| `testing/contracts.py` | Provider, tool, mailbox, filesystem, and isolation contract helpers. |
| `tools/__init__.py` | Tool protocol, registry, built-in, and plugin exports. |

## Loop guards and contract support

| Path | Responsibility |
|---|---|
| `loop_guard/` | Bounded repeated-call/failure/max-turn detection and checkpointable guard state. |
| `execution/` | Unified shell/filesystem execution capability seam and local/remote implementations. |
| `testing/` | Reusable provider, tool, mailbox, filesystem, and isolation contract checks. |

## Explicit extensions

| Path | Responsibility |
|---|---|
| `ext/deep_agent/` | Deep-agent profiles, prompts, factory, and specialist subagent definitions. |
| `ext/evals/` | Scripted providers, evaluation cases/results, benchmark harness, and scorers. |
| `ext/workflow/` | Deterministic workflow context, journal, engine, and suspension/errors. |
| `ext/reports.py` | Run report construction and persisted report loading. |
| `ext/loop_runner.py` | Host-driven recurring-loop runner and filesystem artifacts. |
| `ext/scaffold/` | Stdlib-only project scaffolding console implementation. |
| `ext/__init__.py` | Documents the explicit extension namespace without exporting extension names into `linch.__all__`. |

File memory uses `context/file_memory.py` and `FileBackend`; queue dispatch and
schedules use `linch.worker`. The project-scaffolding CLI is implemented by
`ext/scaffold/__main__.py` and is tested under `tests/cli`; there is no separate
`cli/` package in the runtime tree.

Back to the [architecture index](./README.md).

# Module Inventory

> Part of the [Linch architecture guide](./README.md).

| Module | Responsibility |
|--------|---------------|
| `agent.py` | Neutral SDK configuration; tool-aware system-block assembly; `session()` and safe `fork_session()` factories. A bare agent has no implicit workspace tools or project-local store. |
| `session.py` | Per-conversation state: `session_log`, `run_deps`, `RunOptions`; `provider_view`/`full_history` are read-only projections of `session_log` |
| `session_log.py` | The single append-only `SessionLog` + `MessageEntry`/`ProjectionEntry`; deep-copy projects the session-owned model-visible `provider_view` and audit `full_history` (per-request context is outside the log); compaction is a logged `record_projection` |
| `kernel/` | Dependency-free IoC/effect kernel: `Context` (per-agent scope), `Disposable` (reversible effects), `EffectScope`, `EventBus` (emit/serial/waterfall). See [kernel.md](./kernel.md) |
| `execution/` | Capability seam: `ExecutionBackend` protocol (`shell` + `fs`), `LocalExecutionBackend` (default), `RemoteExecutionBackend` (bundles supplied transports) |
| `loop/` | Turn orchestration (`runner.py`), streaming + `ContextLengthError` recovery (`streaming.py`), `ProviderRequest` assembly (`request.py`), terminal event tails + gate evaluation (`terminals.py`), event persistence + checkpoint serialization (`checkpoint.py`) |
| `types.py` | Shared dataclasses: `Message`, `ContentBlock`, `ProviderRequest`, `OutputSchema` |
| `events.py` | All event dataclasses + round-trip serialization (`event_to_dict` / `event_from_dict`), including stream-only `ToolProgressEvent` |
| `config.py` | `FeatureFlags`, `SystemPromptConfig` |
| `context/` | `ContextBuilder` protocol, `ContextBuildResult`, `ContextBudget`, `apply_context_budget`; consumed by `ContextInjectionHook` in `hooks/adapters.py` |
| `loop_guard/` | `LoopGuard`, `LoopGuardState`, `LoopGuardDecision`, `evaluate_loop_guard`, `normalize_loop_guard` |
| `memory/` | `MemoryStore` protocol, reference stores including `TieredMemoryStore`, `MemoryContextBuilder`, memory tools |
| `filesystem/` | Explicit `FeatureFlags(filesystem=True)` capability: `FileBackend` protocol, `StateFileBackend`, `DiskFileBackend`, `SqliteFileBackend`, `CompositeFileBackend`, `OffloadConfig`, ls/read_file/write_file/edit_file tools |
| `scheduler.py` | Canonical tool-call security pipeline (validate → offered-tool boundary → `PreToolUse` → revalidate → final permission), resource-aware bounded execution, stream-only tool progress, and enabled-filesystem offload at the result chokepoint |
| `compaction.py` | Context-window management; calls `agent.provider` directly; `CompactionLadder` + `micro_compact` recovery rungs |
| `budget.py` | `RunBudget` — token/USD spending caps shared across the agent tree; charged per turn in `loop/runner.py` |
| `workflow/` | Deterministic workflow engine: `WorkflowContext` (`context.py`), content-addressed journal (`journal.py`), `run_workflow` driver (`engine.py`) |
| `coordination/` | Optional capabilities that advance the loop from a clock or a peer: `scheduling/` (cron/interval primitive + `CreateSchedule`/`List`/`Cancel` tools, `SchedulerLoop`), `mailbox/` (peer message bus + `Correlator`), `send_message.py`. Opt-in via `Agent(schedule_store=...)` / `Agent(mailbox=...)` |
| `permissions/` | `PermissionEngine`: final canonical-input rule evaluation, event emission, loop suspension, durable same-turn permission decision keys |
| `pricing.py` | `ModelPricing`, `_DEFAULT_PRICING`, `cost_usd()` for per-turn and cumulative cost events |
| `evals/` | Scripted provider, eval case/result dataclasses, built-in scorers, `run_eval()` |
| `providers/` | `BaseProvider`, `ProviderCapabilities`; implementations: `OpenAIChatCompletionsProvider` (generic OpenAI-compatible endpoint), `DeepSeekProvider` (native thinking, JSON-object output, `reasoning_content` round-trip), `OpenAIResponsesProvider` (stateful, native reasoning effort/summary), `AnthropicProvider` (adaptive/extended thinking with signature, prompt caching), `GeminiProvider`, `LlamaCppProvider`, `VLLMProvider`, `SGLangProvider`; `limiter.py` — `Limiter` protocol and the `provider_slot` gate core holds around every live provider call |
| `tools/` | Tool protocol, `ToolContext`, `ToolRegistry` (registration returns a `Disposable`), `ToolResult`, `Citation`, built-in tools, execution backends; `pipeline.py` — the open `tools/pre-execute → execute → post-execute` seam (`ToolPipeline`); `wrappers/` — `tools/execute` around-wrappers (metrics, timeout) |
| `sessions/` | `SessionStore` protocol, `InMemorySessionStore`, `SqliteSessionStore`, Postgres store, provider-view snapshots, and portable safe-prefix session forking |
| `mcp/` | MCP server connection → Linch tool adapters |
| `skills/` | `SKILL.md`-based slash-commands with argument substitution |
| `subagents/` | Specialized agent roles from `.linch/agents.yaml`; `workers.py` — `WorkerHandle` dataclass for per-worker state tracking; wiring for `SubagentContinueTool` |
| `run_store.py` | `InMemoryRunStore`/`SqliteRunStore`, `RunCheckpoint`, and `RunContract` — durable checkpoints plus a canonical fingerprint of resume-safe execution inputs. Background completion events are status-only audit records; no durable result-delivery queue exists. |
| `deep_agent/` | Explicit `create_deep_agent` profiles/factory (`factory.py`); deep prompt layers (`prompts.py`); specialist roster — researcher, planner, implementer, verification (`subagents.py`) |
| `tools/subagent_continue.py` | `SubagentContinueTool` — continues a retained child session by worker id or display name |
| `tools/subagent_stop.py` | `TaskStopTool` — cancels a background worker; handle remains in `session.workers` and is still continuable |
| `tools/_worker_utils.py` | `resolve_worker` — shared id/display-name lookup helper used by continue and stop tools |
| `recipes/` | *(removed)* — use `Agent(...)` directly; see `examples/` for domain patterns |

## Design rationale

- **One module, one responsibility.** Each row has a single reason to change, so a bug
  or feature touches a bounded surface. The `loop/` package is itself split by
  responsibility (orchestration / streaming / request assembly / terminals /
  checkpoint) for the same reason — the turn loop was too big to be one file.
- **Protocols and implementations live together but stay separable.** A subsystem
  exposes a protocol (`MemoryStore`, `FileBackend`, `SessionStore`, `BaseProvider`) plus
  reference implementations; an embedder can supply its own without forking core.
- **Optional layers are physically grouped.** `coordination/` (and the optional
  `deep_agent/`, `workflow/`, `evals/`) sit apart from the always-on core, so the
  load-bearing loop is easy to find and the opt-in extras don't inflate the mental model
  of "what the SDK does by default."

The virtual filesystem/offload layer is one such explicit capability: set
`FeatureFlags(filesystem=True)` or choose a preset that does so. A bare SDK agent
does not create `.linch` state merely by opening a session.

---

Back to the [architecture index](./README.md).

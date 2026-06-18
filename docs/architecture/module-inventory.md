# Module Inventory

> Part of the [Linch architecture guide](./README.md).

| Module | Responsibility |
|--------|---------------|
| `agent.py` | Immutable config; system block assembly; `session()` factory |
| `session.py` | Per-conversation state: `provider_view`, `full_history`, `run_deps`, `RunOptions` |
| `loop/` | Turn orchestration (`runner.py`), streaming + `ContextLengthError` recovery (`streaming.py`), `ProviderRequest` assembly (`request.py`), terminal event tails + gate evaluation (`terminals.py`), event persistence + checkpoint serialization (`checkpoint.py`) |
| `types.py` | Shared dataclasses: `Message`, `ContentBlock`, `ProviderRequest`, `OutputSchema` |
| `events.py` | All event dataclasses + round-trip serialization (`event_to_dict` / `event_from_dict`) |
| `config.py` | `FeatureFlags`, `SystemPromptConfig` |
| `context/` | `ContextBuilder` protocol, `ContextBuildResult`, `ContextBudget`, `apply_context_budget`; consumed by `ContextInjectionHook` in `hooks/adapters.py` |
| `loop_guard/` | `LoopGuard`, `LoopGuardState`, `LoopGuardDecision`, `evaluate_loop_guard`, `normalize_loop_guard` |
| `memory/` | `MemoryStore` protocol, reference stores including `TieredMemoryStore`, `MemoryContextBuilder`, memory tools |
| `filesystem/` | `FileBackend` protocol, `StateFileBackend`, `DiskFileBackend`, `SqliteFileBackend`, `CompositeFileBackend`, `OffloadConfig`, ls/read_file/write_file/edit_file tools |
| `scheduler.py` | Resource-aware parallel tool execution with concurrency cap; applies `maybe_offload` at the result chokepoint |
| `compaction.py` | Context-window management; calls `agent.provider` directly; `CompactionLadder` + `micro_compact` recovery rungs |
| `budget.py` | `RunBudget` — token/USD spending caps shared across the agent tree; charged per turn in `loop/runner.py` |
| `workflow/` | Deterministic workflow engine: `WorkflowContext` (`context.py`), content-addressed journal (`journal.py`), `run_workflow` driver (`engine.py`) |
| `coordination/` | Optional capabilities that advance the loop from a clock or a peer: `scheduling/` (cron/interval primitive + `CreateSchedule`/`List`/`Cancel` tools, `SchedulerLoop`), `mailbox/` (peer message bus + `Correlator`), `send_message.py`. Opt-in via `Agent(schedule_store=...)` / `Agent(mailbox=...)` |
| `permissions/` | `PermissionEngine`: rule evaluation, event emission, loop suspension, durable permission decision keys |
| `pricing.py` | `ModelPricing`, `_DEFAULT_PRICING`, `cost_usd()` for per-turn and cumulative cost events |
| `evals/` | Scripted provider, eval case/result dataclasses, built-in scorers, `run_eval()` |
| `providers/` | `BaseProvider`, `ProviderCapabilities`; implementations: `OpenAIChatCompletionsProvider` (any OpenAI-compatible endpoint, `reasoning_content` round-trip for DeepSeek/o-series), `OpenAIResponsesProvider` (stateful, native reasoning effort/summary), `AnthropicProvider` (extended thinking with signature, prompt caching), `GeminiProvider`, `LlamaCppProvider`, `VLLMProvider`, `SGLangProvider` |
| `tools/` | Tool protocol, `ToolContext`, `ToolRegistry`, `ToolResult`, `Citation`, built-in tools, execution backends |
| `sessions/` | `SessionStore` protocol, `InMemorySessionStore`, `SqliteSessionStore` |
| `mcp/` | MCP server connection → Linch tool adapters |
| `skills/` | `SKILL.md`-based slash-commands with argument substitution |
| `subagents/` | Specialized agent roles from `.linch/agents.yaml`; `workers.py` — `WorkerHandle` dataclass for per-worker state tracking; wiring for `SubagentContinueTool` |
| `run_store.py` | `SqliteRunStore`, `RunCheckpoint` — durable run-level checkpoint/resume storage; `RunCheckpoint` stores background workers and current-turn permission decisions |
| `deep_agent/` | `create_deep_agent` factory (`factory.py`); deep prompt layers (`prompts.py`); specialist subagent roster — researcher, planner, implementer (`subagents.py`) |
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

---

Back to the [architecture index](./README.md).

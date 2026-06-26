# Examples

[← Usage guide](./README.md)

Runnable example code lives in the `examples/` directory, organized by subsystem. Local demos (marked *local*) run without a live API key, so you can explore the mechanics offline before wiring up a provider.

---

## `core/`

| File | What it shows |
|------|---------------|
| `core/minimal_agent.py` | Smallest possible agent |
| `core/coding_agent.py` | Full SWE agent — tools_from_defaults, BashRule/PathRule safety fence, LoopGuard, multi-turn |
| `core/policy_aware_execution.py` | Docker-backed Bash execution with permission rules and opt-in runtime restrictions |
| `core/reading_agent.py` | Read-only codebase Q&A — exclude Write/Edit/Bash, PathRule, custom reviewer persona |
| `core/chat_agent.py` | Pure conversation agent — no tools, custom domain, structured JSON output via ContextBuilder injection |
| `core/custom_permissions.py` | All permission modes and rule types |
| `core/system_prompts.py` | append, replace, per-session override, persona patterns |
| `core/structured_output.py` | OutputSchema, final_tool_name, JSON extraction |
| `core/event_streaming.py` | Consuming events for SSE, WebSocket, CLI progress |
| `core/multi_session.py` | Web-app pattern: one Agent, many users, shared deps |
| `core/loop_guard_agent.py` | LoopGuard — identical-call and failure-streak detection |
| `core/interactive_cli.py` | Interactive REPL |
| `core/deep_agent_resume.py` | `create_deep_agent` — 4 demos: planning + /memories, background worker + notification, fork/continue, coordinator mode |

---

## `tools/` — *local demos available*

| File | What it shows |
|------|---------------|
| `tools/custom_tools.py` | 5 tool patterns: read, write, exec, parallel, with deps |
| `tools/parallel_search_agent.py` | Scheduler V2: parallel search, resources, concurrency cap |
| `tools/runtime_tools.py` | Runtime registry add/remove/replace/select and schema export |
| `tools/tool_reliability_agent.py` | Timeout, per-tool opt-out (`execution_timeout_ms=0`), `RetryOptions` |
| `tools/rag_tools.py` | RAG tool suite: hybrid_search, keyword_search, graph_search, web_search |
| `tools/filesystem_offload.py` | Virtual filesystem backends, auto-offload of large results (*runs offline*) |

---

## `context/` — *local demos available*

| File | What it shows |
|------|---------------|
| `context/context_injection.py` | ContextInjectionHook patterns: RAG per-turn, budget, selected tools |
| `context/rag_context_builder.py` | ContextInjectionHook RAG with metadata and budget reporting |

---

## `memory/` — *local demos available*

| File | What it shows |
|------|---------------|
| `memory/memory_agent.py` | Core memory primitives with search/upsert tools and citations |
| `memory/sqlite_memory_agent.py` | SqliteMemoryStore — persistent memory, round-trip, upsert update |
| `memory/faiss_adapter.py` | FAISS vector-memory adapter recipe using the existing `MemoryStore` seam |
| `memory/pgvector_memory.py` | pgvector adapter recipe for Postgres semantic search |
| `memory/qdrant_adapter.py` | Qdrant vector-memory adapter recipe |

---

## `observability/`

| File | What it shows |
|------|---------------|
| `observability/observability_agent.py` | LoggingObserver + optional OpenTelemetryObserver |
| `observability/custom_observer.py` | BaseObserver subclass: latency tracking, error counts per tool |

---

## `providers/`

| File | What it shows |
|------|---------------|
| `providers/openai_agent.py` | OpenAIChatCompletionsProvider — basic Q&A, thinking events, tool use, thinking + tool use, structured output, multi-turn |
| `providers/anthropic_agent.py` | AnthropicProvider — basic Q&A, extended thinking (with `PartialAssistantEvent`), prompt caching |
| `providers/deepseek_agent.py` | DeepSeek via both OpenAI-compatible and Anthropic-compatible endpoints — thinking, tool use, thinking + tool use, multi-turn |

---

## `integrations/` — *local demo available*

| File | What it shows |
|------|---------------|
| `integrations/subagent_coordinator.py` | Agent definition files, tool-filtered subagents, SubagentEvent |
| `integrations/multi_agent_isolation.py` | Context isolation: child work never enters parent context; sequential pipeline; parallel analysts; subagent + filesystem offload (*runs offline*) |

## `extensions/` — *templates*

| File | What it shows |
|------|---------------|
| `extensions/provider_template.py` | Minimal `BaseProvider` adapter with normalized streaming events |
| `extensions/memory_store_template.py` | Duck-typed `MemoryStore` with search/upsert signatures |
| `extensions/filesystem_backend_template.py` | Virtual `FileBackend` path operations |
| `extensions/tool_package_template.py` | `@tool` first, with an advanced class-shaped tool plus registry factory |
| `extensions/hook_package_template.py` | Hook object with dispatcher method names and `HookResult` actions |

## `recipes/` — *local demo available*

| File | What it shows |
|------|---------------|
| `recipes/research_desk.py` | A **non-coding** agent (literature analyst): domain tools (`search_library`/`read_article`/`record_citation`), `ctx.deps` corpus + citation ledger, an `OutputSchema` brief, and a closed-loop `Verifier` that bounces an uncited answer — proof the SDK isn't coding-shaped. Built via a factory so it runs offline under a `ScriptedProvider` |
| `recipes/loop_runner.py` | `LoopSpec` + `LoopRunner.run_once()` as the SDK-native outer-loop primitive. Loads project `.env` (`API_KEY`/`BASE_URL`/`model`, or explicit provider keys), writes `domains/<loop_id>/README.md`, `LOG.md`, and per-run report artifacts |
| `recipes/ralph_loop.py` | The **Ralph loop** for long-horizon work: an outer loop where each pass gets a *fresh* `session` (no compaction — discard and restart), reads the same spec, and carries state only through the virtual filesystem (`StateFileBackend`), looping until a done-predicate. A harness pattern composed from existing seams; `max_iterations` bounds the spend |

## `coordination/` — *local demos available*

The optional [coordination layer](./coordination.md) — advancing the loop from a
clock or a peer. Both are built via a factory so they run offline under a
`ScriptedProvider` (smoke-tested in `tests/test_example_coordination.py`).

| File | What it shows |
|------|---------------|
| `coordination/scheduling_agent.py` | An agent that schedules its own recurring work: `Agent(schedule_store=...)` auto-registers `CreateSchedule`, a `SchedulerLoop` fires the due payload into `pending_notifications`, and the next turn drains it as a `<scheduled-task>` |
| `coordination/team_mailbox.py` | A two-agent team over a shared `InMemoryMailbox`: one peer addresses another with `send_message` and it drains into the recipient's next turn, plus a `Correlator` request/response handshake (plan approval) |

---

Built-in subagents are available without disk definitions. After non-trivial implementation or workflow changes, ask the model to invoke `Subagent` with `subagent_type="verification"` and a prompt that includes the original task, artifacts or files changed, approach taken, and checks you expect it to run. The verification subagent is restricted to `Read`, `Glob`, `Grep`, and `Bash` and must end with `VERDICT: PASS`, `VERDICT: FAIL`, or `VERDICT: PARTIAL`.

---

Back to the [Usage guide index](./README.md).

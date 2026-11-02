# Examples

Use this index to find runnable examples for the subsystem you are integrating.

[← Usage guide](./README.md)

Runnable example code lives in the `examples/` directory, organized by subsystem. Local demos (marked *local*) run without a live API key, so you can explore the mechanics offline before wiring up a provider. Some examples also expose offline factories or helpers while their script's `main()` uses a live provider.

---

## `core/`

| File | What it shows |
|------|---------------|
| `core/tiny_agent.py` | Minimal bare `Agent` + session loop; add `workspace_tools()` explicitly when the app needs file or shell access |
| `core/coding_agent.py` | SWE agent with `workspace_tools`, `BashRule`/`PathRule` safety fences, `LoopGuard`, and multi-turn context |
| `core/policy_aware_execution.py` | Docker-backed Bash execution with permission rules and opt-in runtime restrictions |
| `core/reading_agent.py` | Read-only codebase Q&A: exclude Write/Edit/Bash, fence paths with `PathRule`, and use a reviewer persona |
| `core/chat_agent.py` | Tool-free customer-support chat with `ContextBuilder` policy injection, `OutputSchema` JSON replies, and multi-turn context |
| `core/custom_permissions.py` | Permission modes, `ToolRule`/`PathRule`/`BashRule`, and synchronous, asynchronous, and interactive approval callbacks |
| `core/system_prompts.py` | Appending, replacing defaults, custom system blocks, per-session overrides, and persona patterns |
| `core/structured_output.py` | Agent-wide and per-run `OutputSchema`, terminal `final_tool_name`, nested schemas, and parse-error handling |
| `core/multi_session.py` | Web-app pattern: one `Agent`, many users, persistent sessions, per-session dependencies, and metadata |
| `core/loop_guard_agent.py` *local* | Repeated-call loop detection, forced final answers, and the `max_turns` fallback |
| `core/interactive_cli.py` | Interactive terminal REPL with clean agent shutdown |
| `core/governance_redaction.py` *local* | Host-supplied regex rules scrub tool results and the final answer through `RedactionHook` |
| `core/ask_user_agent.py` | `AskUserPlugin`, fail-closed responses, timeout handling, and blocking CLI handlers (factory is offline-testable) |
| `core/budget_capped_agent.py` *local* | Shared `RunBudget` token caps and budget warning/error events with scripted usage turns |
| `core/compaction_ladder_agent.py` *local* | Micro-compaction before forced LLM summarization when a provider reports `ContextLengthError` |
| `core/workflow_fleet.py` | Deterministic workflow fan-out, pipeline stages, shared budgets, and Postgres journal replay after a failure |

---

## `tools/` — *local demos available*

| File | What it shows |
|------|---------------|
| `tools/custom_tools.py` | Five tool patterns: read, write, exec, dependency injection, and a validation-heavy class-shaped tool |
| `tools/runtime_tools.py` *local* | Runtime registry add/remove/replace/select, resource metadata, and schema export; the optional agent call is live |
| `tools/rag_tools.py` | Injected RAG clients for hybrid, keyword, graph, and web search tools |
| `tools/tool_middleware.py` *local* | Middleware that rewrites validated input, blocks sensitive calls, and redacts tool output |

---

## `context/` — *local demos available*

| File | What it shows |
|------|---------------|
| `context/context_injection.py` *local* | ContextInjectionHook patterns: RAG per-turn, budget, selected tools |

---

## `memory/` — *local demos available*

| File | What it shows |
|------|---------------|
| `memory/mem0_recall.py` | mem0-compatible context and terminal hooks for run-scoped recall and successful-turn writes; the host supplies the async memory client |

---

## `observability/`

| File | What it shows |
|------|---------------|
| `observability/observability_agent.py` | Direct OpenTelemetry setup with per-agent `Context` injection and console exporters |

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
| `integrations/redis_mailbox.py` *local* | External `Mailbox` adapter over Redis-list semantics, atomic drain, correlation fields, and `assert_mailbox_contract` with a fake client |
| `integrations/skills_usage.py` *local* | Project skill creation, skill loading/invocation, and lifecycle events with a fake provider |

## `extensions/` — *templates*

| File | What it shows |
|------|---------------|
| `extensions/provider_template.py` | Minimal `BaseProvider` adapter with normalized streaming events |
| `extensions/tool_package_template.py` | `@tool` first, with an advanced class-shaped tool and registry factory |

## `recipes/` — *local demo available*

| File | What it shows |
|------|---------------|
| `recipes/research_desk.py` | Non-coding literature analyst with domain tools, `ctx.deps` corpus/citation ledger, `OutputSchema`, and a `FinalAnswerVerifierHook` retry for uncited answers; its factory is offline-testable and `main()` is live |
| `recipes/loop_runner.py` | `LoopSpec` + `LoopRunner.run_once()` for host-triggered recurring work, project `.env` loading, and `domains/<loop_id>/` artifacts |
| `recipes/runner_recipes.py` *local* | Host-owned cron, webhook, fixed-interval, and CI-gate wrappers over `LoopRunner.run_once()` with a fake provider |
| `recipes/ralph_loop.py` | Long-horizon harness where every iteration gets a fresh session and carries state only through `StateFileBackend` until a done predicate; `max_iterations` bounds spend |

## `coordination/` — *local demos available*

The optional [coordination layer](./coordination.md) provides peer messaging and
request/response correlation. The example's factory and mailbox handshake are
smoke-tested offline with a `ScriptedProvider` in
`tests/examples/test_example_coordination.py`; its `main()` uses a live provider.

| File | What it shows |
|------|---------------|
| `coordination/team_mailbox.py` | A two-agent team over a shared `InMemoryMailbox`: one peer addresses another with `send_message` and it drains into the recipient's next turn, plus a `Correlator` request/response handshake (plan approval) |

---

## `rag/` — *local demos available*

| File | What it shows |
|------|---------------|
| `rag/keyword_connector.py` *local* | Dependency-free `RetrievedChunk` connector that ranks an in-memory corpus by keyword overlap |

---

## `worker/`

| File | What it shows |
|------|---------------|
| `worker/minimal.py` | PgQueuer-backed worker fleet with a scripted provider, Postgres run store, enqueue/worker/event modes, and no model API key |
| `worker/benchmark_enqueue.py` | Postgres enqueue throughput benchmark with isolated schema and commit-time `NOTIFY` coverage |

---

Built-in subagents are available without disk definitions. After non-trivial implementation or workflow changes, ask the model to invoke `Subagent` with `subagent_type="verification"` and a prompt that includes the original task, artifacts or files changed, approach taken, and checks you expect it to run. The verification subagent is restricted to `Read`, `Glob`, `Grep`, and `Bash` and must end with `VERDICT: PASS`, `VERDICT: FAIL`, or `VERDICT: PARTIAL`.

---

Back to the [Usage guide index](./README.md).

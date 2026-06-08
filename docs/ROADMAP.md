# Linch SDK Roadmap

This file is intentionally kept as a lightweight roadmap/status page. Phases 1
through 3 are shipped as of 2026-06-08; future implementation planning should
append a new phase below instead of editing historical status into speculative
plans.

---

## Shipped Phases

### Phase 1 — Provider Output And Cost

**A. Anthropic structured output**

`AnthropicProvider` now declares structured-output support. When
`output_schema` is set, the provider creates a schema tool and the loop treats
that tool as the terminal structured result instead of dispatching it as a real
tool. This preserves multi-turn tool use while still returning
`ResultEvent.structured_output`.

Primary files:
- `src/linch/providers/anthropic.py`
- `src/linch/loop.py`
- `tests/providers/test_anthropic_provider.py`
- `tests/providers/test_structured_output.py`

**B. Per-run cost tracking**

`linch.pricing` provides `ModelPricing` and `cost_usd()`. `UsageEvent` carries
`cost_usd` and `cumulative_cost_usd`; `ResultEvent` carries `total_cost_usd`.
Unknown model IDs return `None` rather than silently reporting zero cost.

Primary files:
- `src/linch/pricing.py`
- `src/linch/events.py`
- `src/linch/loop.py`
- `tests/test_pricing.py`

### Phase 2 — Provider Coverage, Evals, Recovery

**C. Google Gemini provider**

`GeminiProvider` and `GeminiProviderOptions` are exported from
`linch.providers`. The optional `linch[gemini]` extra installs
`google-generativeai`. The provider translates Gemini content parts and
function calls into Linch's normalized stream events, supports structured
output, and declares known Gemini context windows.

Primary files:
- `src/linch/providers/gemini.py`
- `src/linch/providers/__init__.py`
- `pyproject.toml`
- `tests/providers/test_gemini_provider.py`

**D. Evals harness**

`linch.evals` includes deterministic scripted providers, eval case/result
dataclasses, `run_eval()`, and built-in scorers for text, tool calls, schema
validity, and cost budgets.

Primary files:
- `src/linch/evals/`
- `tests/test_evals.py`

**E. Tool failure recovery hints**

`ToolResult.recovery_hint` allows tools and scheduler error paths to provide a
specific recovery instruction. The loop injects one deduplicated recovery
message after all tools in a batch fail, so the next provider turn has a
targeted repair hint without repeated resume-time injection.

Primary files:
- `src/linch/tools/base.py`
- `src/linch/scheduler.py`
- `src/linch/loop.py`
- `tests/loop/test_tool_failure_recovery.py`

### Phase 3 — Durable Long-Running Agents

**F. Durable HITL approval**

`RunCheckpoint.permission_decisions` stores per-turn allow/deny decisions using
canonical permission decision keys. On resume of the same checkpointed turn,
the scheduler replays stored decisions before invoking `canUseTool`; at the
start of a fresh turn, `session.current_turn_permission_decisions` is cleared
to prevent stale approvals from crossing turn boundaries.

Primary files:
- `src/linch/permissions/keys.py`
- `src/linch/run_store.py`
- `src/linch/session.py`
- `src/linch/loop.py`
- `src/linch/scheduler.py`
- `tests/loop/test_run_resume.py`

**G. Hierarchical memory tiers**

`TieredMemoryStore` wraps per-tier stores for working, episodic, and semantic
memory. Writes route by `MemoryItem.metadata["tier"]`; reads query all tiers,
apply tier weights, and deduplicate by `(namespace, id)`. Unknown or malformed
tier metadata falls back to `working`. `TieredMemoryStore` is exported from
`linch.memory` and the root `linch` package.

Primary files:
- `src/linch/memory/tiered.py`
- `src/linch/memory/builder.py`
- `src/linch/memory/__init__.py`
- `src/linch/__init__.py`
- `tests/storage/test_tiered_memory.py`

**H. Sandboxed execution backend seam**

`BashTool` delegates command execution to `LocalBackend` by default and accepts
an injected backend. `DockerBackend` provides a `docker run --rm` implementation
when the Docker daemon is usable. `Agent(execution_backend=...)` replaces an
existing `Bash` tool only; it does not inject shell access into restricted
registries that omit `Bash`.

Primary files:
- `src/linch/tools/execution.py`
- `src/linch/tools/builtin.py`
- `src/linch/agent.py`
- `tests/tools/test_execution_backend.py`

---

## Next Implementation Placeholder

Add Phase 4 here when the next concrete scope is chosen. Candidate areas worth
investigating:

- provider-specific pricing tables beyond Anthropic;
- richer eval reporting output formats;
- Docker backend hardening for container lifecycle, environment forwarding, and
  optional network/filesystem restrictions;
- tiered-memory heuristics tuned from real long-running sessions;
- documented migration notes for legacy tool fields.

# Contributing to Linch

Rules, conventions, and workflow for contributors. Read this before opening a PR.

---

## Setup

```bash
git clone https://github.com/DuyTa506/linch.git
cd linch
pip install ./linch-rust/crates/linch-python   # native extension; needs a Rust toolchain
pip install -e '.[dev,postgres,worker,mcp,anthropic,gemini]'
```

Run the full check suite before and after every change:

```bash
pytest                          # all tests
ruff check . && ruff format --check .   # lint + format
pyright                         # type check
```

Auto-fix most style issues:

```bash
ruff check --fix . && ruff format .
```

---

## Running tests

```bash
pytest                                       # full suite
pytest tests/loop/test_agent_loop.py         # single file
pytest tests/loop/test_agent_loop.py::test_name  # single test
pytest -x                                    # stop on first failure
pytest -k "context"                          # filter by name
```

Live API tests are skipped unless `OPENAI_API_KEY` is set:

```bash
OPENAI_API_KEY="$OPENAI_API_KEY" pytest tests/integration/test_live_api.py
```

The live LoopRunner test loads project `.env`, accepts DeepSeek-compatible
`API_KEY`/`BASE_URL`/`model` keys or explicit provider keys, and requires an
explicit quota-spend opt-in:

```bash
LINCH_RUN_LIVE_LOOP_TESTS=1 pytest tests/integration/test_live_loop_runner.py
```

---

## Code rules

### Tool authoring

Use `@tool` for ordinary function-backed tools. It produces the same
Tool-compatible object consumed by `ToolRegistry`, the scheduler, permissions,
hooks, and providers.

```python
from linch import ToolContext, tool

@tool(description="Search project docs.", tags=("rag",))
async def search_docs(query: str, ctx: ToolContext) -> str:
    return await ctx.deps.docs.search(query)
```

For advanced tools that need custom validation, resource declarations, or richer
execution behavior, implement the duck-typed protocol directly. Do not subclass
anything; the scheduler checks for the protocol shape, not `isinstance`.

```python
# correct for advanced tools
class MyTool:
    name = "my_tool"
    scope = "read"
    parallel = True
    ...

# wrong -- no base class
class MyTool(BaseTool):  # ← don't do this
    ...
```

### No blocking I/O in the core loop

Everything in the `loop/` package, `scheduler.py`, `compaction.py`, and all providers
must be async. Use an asyncio-native client for database I/O (the Postgres
backends use asyncpg). For blocking disk or non-database client calls, use
`run_blocking` from `linch._blocking`: it offloads onto bounded daemon threads
with a per-loop concurrency cap and reliable `call_soon_threadsafe` wakeup.
Avoid the default executor for calls that could hang during interpreter teardown.

### Extension points go through hooks

Cross-cutting behavior — tool-call governance, RAG/context injection, final-answer verification, and stop conditions — is wired through the single `Agent(hooks=[...])` layer (`linch.hooks`), not separate Agent parameters. Runtime telemetry is direct OpenTelemetry source instrumentation; the host owns the SDK and exporter. When adding a new extension, define a hook (or a built-in adapter such as `ToolMiddlewareHook`/`ContextInjectionHook`/`FinalAnswerVerifierHook`/`StopPredicateHook`) rather than threading a new bespoke callback through the loop. A hook method returns `None` or a `HookResult`; it must never assume it can crash the run — the dispatcher swallows exceptions and records a `hook` telemetry event.

### Provider stream contract

`provider.stream()` must yield normalized dicts only — never raw API response objects. The loop is provider-agnostic; if you add a new provider, map its wire format inside the provider module.

### Session history uses one append-only log

`SessionLog` is the source of truth. Never modify `session.full_history` or
`session.provider_view` directly: both are read-only projections. Append through
`await session.append(...)`, which is the only durable path — it writes the
messages to the session store before updating the log. Direct
`SessionLog.append(...)` is an in-memory-only mutation and is lost on reload;
reach for it only when there is no store behind the session. Record compaction
with `session.session_log.record_projection(...)`. Per-request context is ephemeral
and remains outside the log.

### No importing provider-specific types outside providers/

The `loop/` package and `scheduler.py` must not import from `openai_responses.py`, `openai_chat.py`, or `anthropic.py`. Cross-cutting concerns go through `ProviderRequest` and the normalized event dicts.

### system_blocks parity test

`tests/context/test_system_blocks.py` has targeted assertions for the default workspace
protocol clauses. If you intentionally change that wording, update those assertions too.
If a test starts failing there, you may have accidentally changed the prompt contract.

---

## Adding a new primitive

Follow the pattern used for every primitive in this codebase:

1. **Type first** — add the dataclass or TypeAlias to `types.py`. Keep it `slots=True`.
2. **Thread it** — add a field to `ProviderRequest`, then `RunOptions`, then `Agent.__init__`. Precedence: `opts.x if opts.x is not None else agent.x`.
3. **Emit it** — map the new field to the wire format inside each provider module (Chat and Responses may differ — that's expected).
4. **Surface it** — add it to the relevant `Event` dataclass; update `event_to_dict`/`event_from_dict`.
5. **Export it** — add the public symbol to `src/linch/__init__.py`.
6. **Test it** — write a test using a fake provider (see below). Do not require a live API key.

---

## Test conventions

### Use a fake provider

Every unit test that exercises the loop should use a fake provider that yields pre-scripted events. Do not call a live API in unit tests. The live tests in `tests/integration/test_live_api.py` are the exception — they are skipped without a key.

Fake provider pattern (use the normalized dict contract from `providers/base.py` — `"message_end"` carries `stop_reason` and `usage`):

```python
from linch.types import Usage

class FakeProvider:
    def context_window(self, model: str) -> int:
        return 200_000

    async def stream(self, req):
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "Hello"}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(input_tokens=10, output_tokens=5),
        }
```

To fake a thinking + tool-use turn:

```python
async def stream(self, req):
    yield {"type": "message_start", "model": req.model}
    yield {"type": "thinking_delta", "text": "Let me think…"}
    yield {"type": "tool_use_start", "id": "call_1", "name": "Bash"}
    yield {"type": "tool_use_input_delta", "id": "call_1", "json_delta": '{"command":"echo hi"}'}
    yield {"type": "tool_use_end", "id": "call_1"}
    yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
```

### Use InMemorySessionStore

Use `InMemorySessionStore` for ordinary unit tests. Durable restart/conformance tests use Postgres through the isolated `pg_dsn` fixture and `AGENT_KIT_TEST_PG_DSN`.

### Lazy imports in files that co-exist with test_hardening.py

`tests/loop/test_hardening.py::test_import_linch_without_mcp_installed` clears all `linch.*` from `sys.modules` and re-imports. Any test file that imports `linch` at module level will get v1 classes while the loop runs v2 classes — causing `isinstance` failures.

**Rule:** in any test file that creates `Agent`, `Session`, or uses content block types, import `linch` classes **inside the test function body**, not at module level.

```python
# correct
def test_something():
    from linch import Agent
    from linch.sessions import InMemorySessionStore
    ...

# wrong — breaks when tests/loop/test_hardening.py runs in the same session
from linch import Agent  # ← module-level import
```

### No `pytest.raises(SomeImportedError)` with module-level imports

For the same reason: if the exception class was imported at module level, it may be a different identity than the one raised after the `sys.modules` reset. Use `pytest.raises(Exception, match="message")` instead when testing exceptions in files that co-exist with `tests/loop/test_hardening.py`.

### One assertion per concept

Tests should fail at the most specific possible assertion. Avoid mega-tests that check 10 things — split them so a failure points immediately to the broken primitive.

---

## Adding a new provider

1. Add the provider module under `providers/` (for example, `providers/<provider>.py`).
2. Implement `context_window(model) -> int`, `capabilities(model) -> ProviderCapabilities`, and `async def stream(req) -> AsyncIterator[dict]`.
3. Map wire events to the normalized dict contract — see `providers/openai_chat.py` for the Chat Completions pattern, `openai_responses.py` + `providers/openai_responses.py` for the Responses API / stateful pattern, and `providers/gemini.py` for providers whose streamed tool-use format is part-based rather than delta-based.
4. The normalized dict events the loop consumes: `message_start`, `text_delta`, `thinking_delta` (optional, carry `signature` for Anthropic round-trips), `tool_use_start`, `tool_use_input_delta`, `tool_use_end`, `message_end` (carries `stop_reason: StopReason` and `usage: Usage`). Never yield raw SDK objects.
5. For providers that emit `reasoning_content` (DeepSeek, o-series via Chat Completions): yield `{"type": "thinking_delta", "text": chunk}` — `_stream_turn` in `loop/streaming.py` assembles it into a `ThinkingBlock` and round-trips it on subsequent turns automatically.
6. Declare `capabilities()` accurately — `_build_turn_request` uses it to downgrade unsupported fields (clears `cache_prompt` when `prompt_cache=False`, clears `output_schema` when `structured_output=False`, etc.).
7. Export from `src/linch/providers/__init__.py`.
8. Add a test with a mocked HTTP client — do not require a real API key in CI.
9. If the provider uses a different structured-output API, map it inside the provider module, not in the `loop/` package.

---

## Adding a new tool

1. Prefer `@tool` for simple sync or async functions; use `FunctionTool` for explicit/dynamic construction.
2. Choose `scope`: `"read"` (no side effects), `"write"` (creates/modifies files or state), `"exec"` (runs commands or external processes).
3. Set `parallel = True` for read/search tools that can run concurrently.
4. Use `ctx.deps` for shared application state — do not use global variables.
5. Return `ToolResult` when the host app needs `metadata`, `citations`, `truncated`, or `recovery_hint`; plain strings and JSON-like values are fine for simple tools.
6. Use class-based duck-typed tools when you need custom `validate()`, `resources(input) -> list[ResourceAccess]`, or non-trivial `summarize()` behavior.
7. `validate()` should raise `ValueError` with a clear message. It runs before permission checks.
8. `summarize()` should return a single line — it appears in logs and compaction summaries.
9. For process execution, prefer injecting a coherent backend through `BashTool(execution=...)` or `Agent(execution_backend=...)` instead of adding subprocess calls to unrelated tools. `Agent(execution_backend=...)` must not add `Bash` to registries that intentionally exclude it.

---

## Adding a new filesystem backend

The `FileBackend` protocol (`filesystem/backend.py`) is duck-typed — no base class.
Implement six async methods: `read`, `write`, `ls`, `edit`, `exists`, `delete`.

1. Add the backend module under `filesystem/` (for example, `filesystem/<backend>.py`).
2. Use `run_blocking` from `linch._blocking` (or an `asyncio`-native client) for any I/O — never block the event loop. See `DiskFileBackend` for disk I/O via `run_blocking` and `PostgresFileBackend` for native async database I/O.
3. Call `normalize_path(path)` at the entry of every method to canonicalize paths.
4. `read` must raise `FileNotFoundError` for missing paths; `edit` must raise `ValueError` when `old_string` is absent or not unique (and `replace_all=False`); `delete` is a no-op for missing paths.
5. Export from `filesystem/__init__.py` and `src/linch/__init__.py`.
6. Add tests by calling `_exercise(backend)` from `tests/filesystem/test_backends.py` — it is a shared async helper that verifies the full CRUD contract.

---

## Versioning and breaking changes

- This project follows semantic versioning. Patch = bug fix. Minor = new primitive (backward-compatible). Major = breaking change to public API.
- The public API surface is defined by `src/linch/__init__.py`. Anything not exported there is internal.
- `Agent.__init__` uses snake_case keyword arguments; removed camelCase aliases are listed in the [3.0 migration guide](migration-3.0.md).
- When a previously-silent `RunOptions` field becomes active (like `thinking`/`effort` did in 0.2), add a CHANGELOG note — it's a latent behavior change even though the signature doesn't change.

---

## PR checklist

- [ ] `pytest` passes with no new failures
- [ ] `ruff check . && ruff format --check .` clean
- [ ] `pyright` clean on changed modules
- [ ] New public symbols exported from `__init__.py`
- [ ] New primitives have a unit test using a fake provider
- [ ] No module-level `linch` imports in test files (lazy import rule)
- [ ] If workspace protocol text changed, `test_system_blocks.py` assertions updated
- [ ] If a new `FileBackend` was added, `tests/filesystem/test_backends.py` exercises it via `_exercise(backend)`
- [ ] CHANGELOG entry if behavior changed for existing users
- [ ] Background worker tasks are cancelled in both `except AbortError` and `except Exception` handlers if the change touches the `loop/` package or worker spawning
- [ ] New subagent tools (SubagentContinueTool, TaskStopTool) are tested with a fake provider and InMemorySessionStore

---

## Project layout

The source and test tree below is the exact tracked result of `git ls-files src tests` at
this revision. Every entry is a real path; refresh this block when adding or
removing a file. The scaffold CLI is under `src/linch/ext/scaffold/` (with
coverage in `tests/cli/`); there is intentionally no `src/linch/cli/` package.
The tracked `tests/observability/` directory contains OpenTelemetry regression
tests; runtime instrumentation lives in `src/linch/_telemetry.py`.

```text
src/linch/__init__.py
src/linch/_blocking.py
src/linch/_client_lifecycle.py
src/linch/_http_errors.py
src/linch/_json.py
src/linch/_prompt_cache.py
src/linch/_telemetry.py
src/linch/_token_estimation.py
src/linch/_tracing.py
src/linch/_version.py
src/linch/_wire.py
src/linch/abort.py
src/linch/agent.py
src/linch/budget.py
src/linch/compaction.py
src/linch/config.py
src/linch/context/__init__.py
src/linch/context/builder.py
src/linch/context/file_memory.py
src/linch/coordination/__init__.py
src/linch/coordination/_claim_validation.py
src/linch/coordination/mailbox/__init__.py
src/linch/coordination/mailbox/core.py
src/linch/coordination/mailbox/correlation.py
src/linch/coordination/send_message.py
src/linch/durability.py
src/linch/errors.py
src/linch/events.py
src/linch/execution/__init__.py
src/linch/execution/backend.py
src/linch/execution/local.py
src/linch/execution/remote.py
src/linch/ext/__init__.py
src/linch/ext/deep_agent/__init__.py
src/linch/ext/deep_agent/factory.py
src/linch/ext/deep_agent/prompts.py
src/linch/ext/deep_agent/subagents.py
src/linch/ext/evals/__init__.py
src/linch/ext/evals/benchmark.py
src/linch/ext/evals/harness.py
src/linch/ext/evals/scorers.py
src/linch/ext/evals/scripted.py
src/linch/ext/loop_runner.py
src/linch/ext/reports.py
src/linch/ext/scaffold/__init__.py
src/linch/ext/scaffold/__main__.py
src/linch/ext/scaffold/_scaffold.py
src/linch/ext/scaffold/_templates.py
src/linch/ext/workflow/__init__.py
src/linch/ext/workflow/context.py
src/linch/ext/workflow/engine.py
src/linch/ext/workflow/journal.py
src/linch/filesystem/__init__.py
src/linch/filesystem/backend.py
src/linch/filesystem/disk.py
src/linch/filesystem/offload.py
src/linch/filesystem/postgres.py
src/linch/filesystem/tools.py
src/linch/hooks/__init__.py
src/linch/hooks/adapters.py
src/linch/hooks/contexts.py
src/linch/hooks/dispatcher.py
src/linch/hooks/read_before_write.py
src/linch/hooks/redaction.py
src/linch/hooks/tool_cache.py
src/linch/hooks/types.py
src/linch/kernel/__init__.py
src/linch/kernel/context.py
src/linch/kernel/effects.py
src/linch/kernel/errors.py
src/linch/kernel/events.py
src/linch/kernel/plugins.py
src/linch/lifecycle.py
src/linch/loop/__init__.py
src/linch/loop/checkpoint.py
src/linch/loop/contracts.py
src/linch/loop/dispatch.py
src/linch/loop/feedback.py
src/linch/loop/finalize.py
src/linch/loop/provider_turn.py
src/linch/loop/reliability.py
src/linch/loop/request.py
src/linch/loop/resume.py
src/linch/loop/runner.py
src/linch/loop/runtime.py
src/linch/loop/startup.py
src/linch/loop/streaming.py
src/linch/loop/terminals.py
src/linch/loop/tool_batch.py
src/linch/loop/turn_stopping.py
src/linch/loop_guard/__init__.py
src/linch/loop_guard/guard.py
src/linch/mcp/__init__.py
src/linch/mcp/client.py
src/linch/mcp/config.py
src/linch/mcp/naming.py
src/linch/mcp/permission_bridge.py
src/linch/mcp/result.py
src/linch/mcp/tool.py
src/linch/middleware.py
src/linch/model_calls.py
src/linch/openai_responses.py
src/linch/permissions/__init__.py
src/linch/permissions/engine.py
src/linch/permissions/keys.py
src/linch/permissions/principal.py
src/linch/permissions/rules.py
src/linch/permissions/ruleset.py
src/linch/pricing.py
src/linch/profiles.py
src/linch/providers/__init__.py
src/linch/providers/anthropic.py
src/linch/providers/base.py
src/linch/providers/catalog.py
src/linch/providers/deepseek.py
src/linch/providers/gemini.py
src/linch/providers/limiter.py
src/linch/providers/llamacpp.py
src/linch/providers/openai_chat.py
src/linch/providers/openai_responses.py
src/linch/providers/retry.py
src/linch/providers/sglang.py
src/linch/providers/vllm.py
src/linch/rag/__init__.py
src/linch/recovery.py
src/linch/reliability.py
src/linch/run_store.py
src/linch/run_stores/__init__.py
src/linch/run_stores/postgres.py
src/linch/scheduler.py
src/linch/session.py
src/linch/session_log.py
src/linch/sessions/__init__.py
src/linch/sessions/fork.py
src/linch/sessions/memory.py
src/linch/sessions/postgres.py
src/linch/sessions/store.py
src/linch/sessions/tasks.py
src/linch/skills/__init__.py
src/linch/skills/builtins.py
src/linch/skills/listing.py
src/linch/skills/loader.py
src/linch/skills/overlay.py
src/linch/skills/shell_split.py
src/linch/skills/substitute.py
src/linch/skills/system_reminder.py
src/linch/skills/types.py
src/linch/storage/__init__.py
src/linch/storage/_pg.py
src/linch/subagents/__init__.py
src/linch/subagents/builtins.py
src/linch/subagents/default_agent.py
src/linch/subagents/generator.py
src/linch/subagents/loader.py
src/linch/subagents/registry.py
src/linch/subagents/runner.py
src/linch/subagents/types.py
src/linch/subagents/workers.py
src/linch/testing/__init__.py
src/linch/testing/contracts.py
src/linch/tools/__init__.py
src/linch/tools/_worker_utils.py
src/linch/tools/ask_user.py
src/linch/tools/base.py
src/linch/tools/builtin.py
src/linch/tools/execution.py
src/linch/tools/file_tracker.py
src/linch/tools/function.py
src/linch/tools/isolation.py
src/linch/tools/pipeline.py
src/linch/tools/registry.py
src/linch/tools/search.py
src/linch/tools/skill.py
src/linch/tools/subagent.py
src/linch/tools/subagent_continue.py
src/linch/tools/subagent_stop.py
src/linch/tools/tasks.py
src/linch/tools/wrappers/__init__.py
src/linch/tools/wrappers/metrics.py
src/linch/tools/wrappers/timeout.py
src/linch/types.py
src/linch/verification.py
src/linch/worker/__init__.py
src/linch/worker/dispatch.py
src/linch/worker/runtime.py
src/linch/worker/schedules.py
src/linch/worker/tools.py
tests/cli/test_scaffold_cli.py
tests/conftest.py
tests/context/test_context_builder.py
tests/context/test_context_trim_equivalence.py
tests/context/test_file_memory.py
tests/context/test_system_blocks.py
tests/execution/__init__.py
tests/execution/test_execution_backend.py
tests/filesystem/test_backends.py
tests/filesystem/test_offload.py
tests/filesystem/test_scoped_backend.py
tests/filesystem/test_tools.py
tests/fixtures/public_consumer.py
tests/hooks/test_read_before_write.py
tests/hooks/test_redaction.py
tests/hooks/test_tool_cache.py
tests/integration/test_live_api.py
tests/integration/test_live_deepseek.py
tests/integration/test_live_loop_runner.py
tests/kernel/__init__.py
tests/kernel/test_context.py
tests/kernel/test_effects.py
tests/kernel/test_events.py
tests/kernel/test_import_boundary.py
tests/kernel/test_lifecycle.py
tests/kernel/test_plugins.py
tests/loop/test_active_tool_prompt_v2.py
tests/loop/test_agent_loop.py
tests/loop/test_alignment.py
tests/loop/test_alignment_durability.py
tests/loop/test_checkpoint_growth.py
tests/loop/test_checkpointable_hooks.py
tests/loop/test_compaction_ladder.py
tests/loop/test_compaction_provider_agnostic.py
tests/loop/test_compaction_read_tracker.py
tests/loop/test_compaction_response_chaining.py
tests/loop/test_exact_model_input.py
tests/loop/test_execution_world_run_contract.py
tests/loop/test_hardening.py
tests/loop/test_history_reads.py
tests/loop/test_inbox.py
tests/loop/test_initial_checkpoint_recovery.py
tests/loop/test_lease_loss.py
tests/loop/test_loop_event_characterization.py
tests/loop/test_loop_guard.py
tests/loop/test_loop_trace_characterization.py
tests/loop/test_model_call_accounting.py
tests/loop/test_model_fallback.py
tests/loop/test_phase_atomic_checkpoint.py
tests/loop/test_phase_state_recovery.py
tests/loop/test_principal_run_contract.py
tests/loop/test_progress_transient_v2.py
tests/loop/test_provider_limiter.py
tests/loop/test_provider_stream_contract_v2.py
tests/loop/test_recovery_cursor.py
tests/loop/test_run_contract_integration_v2.py
tests/loop/test_run_event_batch.py
tests/loop/test_run_event_buffer.py
tests/loop/test_run_resume.py
tests/loop/test_store_failures.py
tests/loop/test_tool_failure_recovery.py
tests/loop/test_tool_pipeline_run_contract.py
tests/loop/test_truncation_recovery.py
tests/loop/test_verification.py
tests/mailbox/__init__.py
tests/mailbox/test_mailbox.py
tests/mailbox/test_mailbox_tool.py
tests/mcp/test_client.py
tests/mcp/test_mcp_api_contract.py
tests/mcp/test_permission_bridge.py
tests/observability/conftest.py
tests/observability/test_otel_fast_path.py
tests/observability/test_otel_propagation.py
tests/observability/test_otel_runtime.py
tests/observability/test_telemetry.py
tests/observability/test_trace_id.py
tests/permissions/test_principal_roles.py
tests/permissions/test_rules.py
tests/permissions/test_ruleset.py
tests/providers/test_anthropic_provider.py
tests/providers/test_client_loop_affinity.py
tests/providers/test_deepseek_provider.py
tests/providers/test_gemini_provider.py
tests/providers/test_llamacpp_provider.py
tests/providers/test_openai_chat_stream.py
tests/providers/test_openai_responses.py
tests/providers/test_parity_surface.py
tests/providers/test_provider_capabilities.py
tests/providers/test_provider_catalog.py
tests/providers/test_provider_conformance.py
tests/providers/test_retry.py
tests/providers/test_sglang_provider.py
tests/providers/test_structured_output.py
tests/providers/test_vllm_provider.py
tests/rag/test_rag.py
tests/rag/test_terminals_resume.py
tests/skills/test_builtins.py
tests/skills/test_skill_offload.py
tests/skills/test_substitute.py
tests/storage/test_durable_inbox.py
tests/storage/test_model_call_journal.py
tests/storage/test_model_input_snapshots.py
tests/storage/test_pending_run_options.py
tests/storage/test_postgres.py
tests/storage/test_postgres_allocation.py
tests/storage/test_postgres_lifecycle.py
tests/storage/test_postgres_run_store.py
tests/storage/test_provider_snapshot.py
tests/storage/test_reliability_crash.py
tests/storage/test_run_contract.py
tests/storage/test_run_store.py
tests/storage/test_run_store_conformance.py
tests/storage/test_run_store_leases.py
tests/storage/test_schema_versions.py
tests/storage/test_serialization_versioning.py
tests/storage/test_session_fork.py
tests/storage/test_sessions.py
tests/storage/test_task_coordination.py
tests/storage/test_terminal_atomicity.py
tests/storage/test_wire_versioning.py
tests/subagents/test_budget_shared.py
tests/subagents/test_fork_mode.py
tests/subagents/test_generator.py
tests/subagents/test_permission_bubbling.py
tests/subagents/test_registry.py
tests/subagents/test_runner_signals.py
tests/subagents/test_subagent_isolation.py
tests/subagents/test_subagent_offload.py
tests/subagents/test_subagent_structured_output.py
tests/test_abort.py
tests/test_backpressure.py
tests/test_blocking.py
tests/test_budget.py
tests/test_claim_validation.py
tests/test_compaction_accounting.py
tests/test_deep_agent.py
tests/test_deep_agent_storage_config.py
tests/test_docs_references.py
tests/test_eval_benchmark.py
tests/test_evals.py
tests/test_event_catalog.py
tests/test_event_forward_compat.py
tests/test_example_coordination.py
tests/test_example_interaction.py
tests/test_example_ralph_loop.py
tests/test_example_redis_mailbox.py
tests/test_example_research_desk.py
tests/test_example_runner_recipes.py
tests/test_extension_contracts.py
tests/test_extension_templates.py
tests/test_hooks.py
tests/test_idle_sessions.py
tests/test_input_aware_concurrency.py
tests/test_json_policy_equivalence.py
tests/test_lazy_imports.py
tests/test_live_prompt_cache_benchmark.py
tests/test_loop_runner.py
tests/test_mem0_recall_example.py
tests/test_model_calls.py
tests/test_multitenancy.py
tests/test_pricing.py
tests/test_profiles.py
tests/test_prompt_cache_advisory.py
tests/test_prompt_cache_benchmark.py
tests/test_public_api.py
tests/test_reliability.py
tests/test_reports.py
tests/test_run_report_script.py
tests/test_runtime_benchmark.py
tests/test_runtime_benchmark_scale.py
tests/test_scheduling_removed.py
tests/test_session_durability.py
tests/test_session_lifecycle.py
tests/test_session_log.py
tests/test_token_estimator_policies.py
tests/tools/test_ask_user.py
tests/tools/test_background_tool.py
tests/tools/test_builtin_search.py
tests/tools/test_execution_backend.py
tests/tools/test_function_tools.py
tests/tools/test_idempotency_key.py
tests/tools/test_isolation.py
tests/tools/test_maximal_batching.py
tests/tools/test_middleware.py
tests/tools/test_pipeline.py
tests/tools/test_principal_context.py
tests/tools/test_registry_disposer.py
tests/tools/test_registry_ext.py
tests/tools/test_resolve.py
tests/tools/test_scheduler_security_v2.py
tests/tools/test_scheduler_tool_output_v2.py
tests/tools/test_scheduler_v2.py
tests/tools/test_search.py
tests/tools/test_subagent_parallel.py
tests/tools/test_subagent_tool.py
tests/tools/test_tool_deps.py
tests/tools/test_tool_output_v2_foundation.py
tests/tools/test_tool_reliability.py
tests/tools/test_tool_result_v2.py
tests/worker/conftest.py
tests/worker/test_coordination.py
tests/worker/test_dispatch.py
tests/worker/test_initial_failure.py
tests/worker/test_load_test.py
tests/worker/test_outage.py
tests/worker/test_ownership.py
tests/worker/test_recovery.py
tests/worker/test_schedules.py
tests/worker/test_store_lifetime.py
tests/worker/test_worker_profiles.py
tests/workflow/test_context.py
tests/workflow/test_engine.py
tests/workflow/test_journal.py
tests/workflow/test_principal_fingerprint.py
```

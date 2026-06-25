# Contributing to Linch

Rules, conventions, and workflow for contributors. Read this before opening a PR.

---

## Setup

```bash
git clone https://github.com/DuyTa506/linch.git
cd linch
pip install -e '.[dev,mcp,anthropic]'
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

Everything in the `loop/` package, `scheduler.py`, `compaction.py`, and all providers must be async. For a blocking sync call (disk, CPU, a long-lived `sqlite3` connection) use `run_blocking` from `linch._blocking` — it offloads onto a **bounded daemon thread** (per-loop concurrency cap, reliable `call_soon_threadsafe` wakeup) so the event loop is never blocked and a hung call never blocks interpreter/test teardown. For a long-lived `sqlite3` connection use the `SqliteExecutor` helper (`linch.storage._executor.SqliteExecutor`): it serializes all access behind a lock and runs the work through `run_blocking`, so only one operation touches the connection at a time. Prefer these over `asyncio.to_thread` / `loop.run_in_executor(None, ...)`, which use the loop's default `ThreadPoolExecutor`: those threads may be non-daemon (causing interpreter/test teardown to hang on a stuck call) and have different per-loop lifecycle and queueing semantics compared to the bounded per-loop concurrency cap and daemon-thread teardown that `run_blocking` provides.

### Extension points go through hooks

Cross-cutting behavior — telemetry, tool-call governance, RAG/context injection, final-answer verification, stop conditions — is wired through the single `Agent(hooks=[...])` layer (`linch.hooks`), not separate Agent parameters. When adding a new extension, define a hook (or a built-in adapter such as `RunTelemetryHook`/`ToolMiddlewareHook`/`ContextInjectionHook`/`FinalAnswerVerifierHook`/`StopPredicateHook`) rather than threading a new bespoke callback through the loop. A hook method returns `None` or a `HookResult`; it must never assume it can crash the run — the dispatcher swallows exceptions and records a `hook` telemetry event.

### Provider stream contract

`provider.stream()` must yield normalized dicts only — never raw API response objects. The loop is provider-agnostic; if you add a new provider, map its wire format inside the provider module.

### `full_history` is append-only

Never modify `session.full_history` outside the `loop/` package. It is the audit log. Only `session.provider_view` may be pruned or summarized.

### No importing provider-specific types outside providers/

The `loop/` package and `scheduler.py` must not import from `openai_responses.py`, `openai_chat.py`, or `anthropic.py`. Cross-cutting concerns go through `ProviderRequest` and the normalized event dicts.

### system_blocks parity test

`tests/context/test_system_blocks.py` has a byte-identical assertion on the default SWE system-block text. If you intentionally change the wording, update that assertion too. If a test starts failing there, you've accidentally changed the prompt.

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

Use `InMemorySessionStore` in all tests. `SqliteSessionStore` writes to disk and makes teardown harder.

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

1. Create `src/linch/providers/my_provider.py`.
2. Implement `context_window(model) -> int`, `capabilities(model) -> ProviderCapabilities`, and `async def stream(req) -> AsyncIterator[dict]`.
3. Map wire events to the normalized dict contract — see `providers/openai_chat.py` for the Chat Completions pattern, `openai_responses.py` + `providers/openai_responses.py` for the Responses API / stateful pattern, and `providers/gemini.py` for providers whose streamed tool-use format is part-based rather than delta-based.
4. The normalized dict events the loop consumes: `message_start`, `text_delta`, `thinking_delta` (optional, carry `signature` for Anthropic round-trips), `tool_use_start`, `tool_use_input_delta`, `tool_use_end`, `message_end` (carries `stop_reason: StopReason` and `usage: Usage`). Never yield raw SDK objects.
5. For providers that emit `reasoning_content` (DeepSeek, o-series via Chat Completions): yield `{"type": "thinking_delta", "text": chunk}` — `stream_turn` in `loop/streaming.py` assembles it into a `ThinkingBlock` and round-trips it on subsequent turns automatically.
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
9. For process execution, prefer injecting a backend into `BashTool` or `Agent(execution_backend=...)` instead of adding subprocess calls to unrelated tools. `Agent(execution_backend=...)` must not add `Bash` to registries that intentionally exclude it.

---

## Adding a new filesystem backend

The `FileBackend` protocol (`filesystem/backend.py`) is duck-typed — no base class.
Implement six async methods: `read`, `write`, `ls`, `edit`, `exists`, `delete`.

1. Create `src/linch/filesystem/my_backend.py`.
2. Use `run_blocking` from `linch._blocking` (or an `asyncio`-native client) for any I/O — never block the event loop. For a SQLite backend, use `SqliteExecutor` from `linch.storage._executor` (lock-serialized, work offloaded via `run_blocking`). See `DiskFileBackend` (disk I/O via `run_blocking`) and `SqliteFileBackend` (`SqliteExecutor`) for reference patterns.
3. Call `normalize_path(path)` at the entry of every method to canonicalize paths.
4. `read` must raise `FileNotFoundError` for missing paths; `edit` must raise `ValueError` when `old_string` is absent or not unique (and `replace_all=False`); `delete` is a no-op for missing paths.
5. Export from `filesystem/__init__.py` and `src/linch/__init__.py`.
6. Add tests by calling `_exercise(backend)` from `tests/filesystem/test_backends.py` — it is a shared async helper that verifies the full CRUD contract.

---

## Versioning and breaking changes

- This project follows semantic versioning. Patch = bug fix. Minor = new primitive (backward-compatible). Major = breaking change to public API.
- The public API surface is defined by `src/linch/__init__.py`. Anything not exported there is internal.
- `dict`/`camelCase` kwargs on `Agent.__init__` are supported for backward compatibility. New parameters use snake_case keyword arguments only.
- When a previously-silent `RunOptions` field becomes active (like `thinking`/`effort` did in 0.2), add a CHANGELOG note — it's a latent behavior change even though the signature doesn't change.

---

## PR checklist

- [ ] `pytest` passes with no new failures
- [ ] `ruff check . && ruff format --check .` clean
- [ ] `pyright` clean on changed modules
- [ ] New public symbols exported from `__init__.py`
- [ ] New primitives have a unit test using a fake provider
- [ ] No module-level `linch` imports in test files (lazy import rule)
- [ ] If system-block text changed, `test_system_blocks.py` parity assertion updated
- [ ] If a new `FileBackend` was added, `tests/filesystem/test_backends.py` exercises it via `_exercise(backend)`
- [ ] CHANGELOG entry if behavior changed for existing users
- [ ] Background worker tasks are cancelled in both `except AbortError` and `except Exception` handlers if the change touches the `loop/` package or worker spawning
- [ ] New subagent tools (SubagentContinueTool, TaskStopTool) are tested with a fake provider and InMemorySessionStore

---

## Project layout

```
src/linch/          core library
  agent.py              config object + session factory
  loop/                 main agent loop package — runner, streaming, request
                        assembly, terminal tails/gates, checkpointing
  session.py            per-conversation state + RunOptions
  types.py              all shared dataclasses
  events.py             event dataclasses + serialization
  config.py             FeatureFlags, SystemPromptConfig
  hooks/                unified extension layer — HookEvent chokepoints,
                        HookResult/HookDispatcher, built-in adapters
  _blocking.py          run_blocking: bounded daemon-thread offload for sync I/O
  context/              ContextBuilder protocol + budget/result types
  memory/               MemoryStore protocol + reference RAG primitives
  filesystem/           FileBackend protocol + State/Disk/SQLite/Composite backends
                        + ls/read_file/write_file/edit_file tools + OffloadConfig
  scheduler.py          resource-aware parallel tool execution
  compaction.py         context-window management
  loop_guard/           agentic-loop detection (identical-call / failure streaks)
  verification.py       Verifier protocol + ScorerVerifier (wired via hooks)
  budget.py             RunBudget token/USD caps shared across the agent tree
  permissions/          PermissionEngine + rule types
  providers/            BaseProvider + OpenAI Chat/Responses/Anthropic/Gemini/llama.cpp/Retry
  tools/                tool protocol, ToolContext, ToolRegistry, built-ins
                        + SubagentContinueTool, TaskStopTool, function tools
  sessions/             SessionStore + InMemory + SQLite
  storage/              SqliteExecutor (lock-serialized sqlite, off-loop)
  mcp/                  MCP server adapters
  skills/               SKILL.md loader + skill context reminders
  subagents/            agents.yaml loader + child agent runner + WorkerHandle (workers.py)
  workflow/             run_workflow engine — WorkflowContext, content-addressed journal
  observability/        RunObserver protocol + Logging/SpanCollector/OpenTelemetry (via RunTelemetryHook)
  evals/                offline harness — ScriptedProvider, run_eval, scorers
  reports.py            build_run_report / load_run_report read models
  pricing.py            USD cost table + cost_usd()
  run_store.py          SqliteRunStore + RunCheckpoint; durable run checkpoint/resume
  deep_agent/           create_deep_agent factory; DEEP_AGENT_SYSTEM_PROMPT, COORDINATOR_SYSTEM_PROMPT; subagent roster

tests/
  context/              context builders + system-block parity (test_system_blocks.py)
  filesystem/           backends, tools, offload unit + end-to-end tests
  loop/                 agent loop, hardening, verification tests
  tools/                tool registry, scheduler, reliability, middleware tests
  storage/              memory + SqliteRunStore tests
  workflow/             workflow engine/context/journal tests
  observability/        observer/hook telemetry tests
  integration/          live-API tests (skipped without a key)
  test_hooks.py         hooks dispatcher/adapters/chokepoints
  test_deep_agent.py    deep agent preset tests
  ...                   (one directory per subsystem)
examples/               runnable scripts (require OPENAI_API_KEY unless marked offline)
docs/                   this documentation (usage/ topic folder, architecture, roadmap)
```

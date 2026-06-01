# Contributing to AgentKit

Rules, conventions, and workflow for contributors. Read this before opening a PR.

---

## Setup

```bash
git clone <repo>
cd agent_kit
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
pytest                                      # full suite
pytest tests/test_agent_loop.py            # single file
pytest tests/test_agent_loop.py::test_name # single test
pytest -x                                   # stop on first failure
pytest -k "context"                         # filter by name
```

Live API tests are skipped unless `OPENAI_API_KEY` is set:

```bash
OPENAI_API_KEY="$OPENAI_API_KEY" pytest tests/test_live_api.py
```

---

## Code rules

### No base classes for tools

Tools are **duck-typed protocols** — do not subclass anything. Implement the five attributes and three methods directly on your class. The scheduler checks for the protocol shape, not `isinstance`.

```python
# correct
class MyTool:
    name = "my_tool"
    scope = "read"
    parallel = True
    parallel_safe = True
    ...

# wrong — no base class
class MyTool(BaseTool):  # ← don't do this
    ...
```

### No blocking I/O in the core loop

Everything in `loop.py`, `scheduler.py`, `compaction.py`, and all providers must be async. Use `asyncio.to_thread()` if you need to call a sync library (see `SqliteSessionStore` for the pattern).

### Provider stream contract

`provider.stream()` must yield normalized dicts only — never raw API response objects. The loop is provider-agnostic; if you add a new provider, map its wire format inside the provider module.

### `full_history` is append-only

Never modify `session.full_history` outside `loop.py`. It is the audit log. Only `session.provider_view` may be pruned or summarized.

### No importing provider-specific types outside providers/

`loop.py` and `scheduler.py` must not import from `openai_responses.py`, `openai_chat.py`, or `anthropic.py`. Cross-cutting concerns go through `ProviderRequest` and the normalized event dicts.

### system_blocks parity test

`tests/test_system_blocks.py` has a byte-identical assertion on the default SWE system-block text. If you intentionally change the wording, update that assertion too. If a test starts failing there, you've accidentally changed the prompt.

---

## Adding a new primitive

Follow the pattern used for every primitive in this codebase:

1. **Type first** — add the dataclass or TypeAlias to `types.py`. Keep it `slots=True`.
2. **Thread it** — add a field to `ProviderRequest`, then `RunOptions`, then `Agent.__init__`. Precedence: `opts.x if opts.x is not None else agent.x`.
3. **Emit it** — map the new field to the wire format inside each provider module (Chat and Responses may differ — that's expected).
4. **Surface it** — add it to the relevant `Event` dataclass; update `event_to_dict`/`event_from_dict`.
5. **Export it** — add the public symbol to `src/agent_kit/__init__.py`.
6. **Test it** — write a test using a fake provider (see below). Do not require a live API key.

---

## Test conventions

### Use a fake provider

Every unit test that exercises the loop should use a fake provider that yields pre-scripted events. Do not call a live API in unit tests. The live tests in `tests/test_live_api.py` are the exception — they are skipped without a key.

Fake provider pattern:

```python
class FakeProvider:
    def context_window(self, model: str) -> int:
        return 200_000

    async def stream(self, req):
        yield {"type": "text_delta", "text": "Hello"}
        yield {"type": "stop", "stop_reason": "end_turn"}
        yield {"type": "usage", "input_tokens": 10, "output_tokens": 5}
```

### Use InMemorySessionStore

Use `InMemorySessionStore` in all tests. `SqliteSessionStore` writes to disk and makes teardown harder.

### Lazy imports in files that co-exist with test_hardening.py

`tests/test_hardening.py::test_import_agent_kit_without_mcp_installed` clears all `agent_kit.*` from `sys.modules` and re-imports. Any test file that imports `agent_kit` at module level will get v1 classes while the loop runs v2 classes — causing `isinstance` failures.

**Rule:** in any test file that creates `Agent`, `Session`, or uses content block types, import `agent_kit` classes **inside the test function body**, not at module level.

```python
# correct
def test_something():
    from agent_kit import Agent
    from agent_kit.sessions import InMemorySessionStore
    ...

# wrong — breaks when test_hardening.py runs in the same session
from agent_kit import Agent  # ← module-level import
```

### No `pytest.raises(SomeImportedError)` with module-level imports

For the same reason: if the exception class was imported at module level, it may be a different identity than the one raised after the `sys.modules` reset. Use `pytest.raises(Exception, match="message")` instead when testing exceptions in files that co-exist with `test_hardening.py`.

### One assertion per concept

Tests should fail at the most specific possible assertion. Avoid mega-tests that check 10 things — split them so a failure points immediately to the broken primitive.

---

## Adding a new provider

1. Create `src/agent_kit/providers/my_provider.py`.
2. Implement `context_window(model) -> int` and `async def stream(req) -> AsyncIterator[dict]`.
3. Map wire events to the normalized dict contract (see `providers/openai_chat.py` for reference).
4. Export from `src/agent_kit/providers/__init__.py`.
5. Add a test with a mocked HTTP client — do not require a real API key in CI.
6. If the provider uses a different structured-output API, handle it in `_build_turn_request` inside the provider, not in `loop.py`.

---

## Adding a new tool

1. Implement the duck-typed protocol (no base class).
2. Choose `scope`: `"read"` (no side effects), `"write"` (creates/modifies files or state), `"exec"` (runs commands or external processes).
3. Set `parallel = True` for read/search tools that can run concurrently. Keep `parallel_safe = True` too when supporting older integrations.
4. Add `resources(input) -> list[ResourceAccess]` when the tool touches files, indexes, databases, tenant state, or other shared resources. Read/read can overlap; write conflicts serialize.
5. Use `ctx.deps` for shared application state — do not use global variables.
6. Return `ToolResult` with `metadata`, `citations`, and `truncated` when the host app needs provenance or rich rendering.
7. `validate()` should raise `ValueError` with a clear message. It runs before permission checks.
8. `summarize()` should return a single line — it appears in logs and compaction summaries.

---

## Adding a new filesystem backend

The `FileBackend` protocol (`filesystem/backend.py`) is duck-typed — no base class.
Implement six async methods: `read`, `write`, `ls`, `edit`, `exists`, `delete`.

1. Create `src/agent_kit/filesystem/my_backend.py`.
2. Use `asyncio.to_thread()` or an `asyncio`-native client for any I/O — never block the event loop. See `DiskFileBackend` (disk I/O) and `SqliteFileBackend` (thread executor) for reference patterns.
3. Call `normalize_path(path)` at the entry of every method to canonicalize paths.
4. `read` must raise `FileNotFoundError` for missing paths; `edit` must raise `ValueError` when `old_string` is absent or not unique (and `replace_all=False`); `delete` is a no-op for missing paths.
5. Export from `filesystem/__init__.py` and `src/agent_kit/__init__.py`.
6. Add tests by calling `_exercise(backend)` from `tests/filesystem/test_backends.py` — it is a shared async helper that verifies the full CRUD contract.

---

## Versioning and breaking changes

- This project follows semantic versioning. Patch = bug fix. Minor = new primitive (backward-compatible). Major = breaking change to public API.
- The public API surface is defined by `src/agent_kit/__init__.py`. Anything not exported there is internal.
- `dict`/`camelCase` kwargs on `Agent.__init__` are supported for backward compatibility. New parameters use snake_case keyword arguments only.
- When a previously-silent `RunOptions` field becomes active (like `thinking`/`effort` did in 0.2), add a CHANGELOG note — it's a latent behavior change even though the signature doesn't change.

---

## PR checklist

- [ ] `pytest` passes with no new failures
- [ ] `ruff check . && ruff format --check .` clean
- [ ] `pyright` clean on changed modules
- [ ] New public symbols exported from `__init__.py`
- [ ] New primitives have a unit test using a fake provider
- [ ] No module-level `agent_kit` imports in test files (lazy import rule)
- [ ] If system-block text changed, `test_system_blocks.py` parity assertion updated
- [ ] If a new `FileBackend` was added, `tests/filesystem/test_backends.py` exercises it via `_exercise(backend)`
- [ ] CHANGELOG entry if behavior changed for existing users

---

## Project layout

```
src/agent_kit/          core library
  agent.py              config object + session factory
  loop.py               main agent loop
  session.py            per-conversation state + RunOptions
  types.py              all shared dataclasses
  events.py             event dataclasses + serialization
  config.py             FeatureFlags, SystemPromptConfig
  context/              ContextBuilder protocol + budget/result types
  memory/               MemoryStore protocol + reference RAG primitives
  filesystem/           FileBackend protocol + State/Disk/SQLite/Composite backends
                        + ls/read_file/write_file/edit_file tools + OffloadConfig
  scheduler.py          resource-aware parallel tool execution
  compaction.py         context-window management
  permissions/          PermissionEngine + rule types
  providers/            BaseProvider + OpenAI Chat/Responses/Anthropic/Retry
  tools/                tool protocol, ToolContext, ToolRegistry, built-ins
  sessions/             SessionStore + InMemory + SQLite
  mcp/                  MCP server adapters
  skills/               SKILL.md loader + skill context reminders
  subagents/            agents.yaml loader + child agent runner
  recipes/              factory helpers (additive, not part of the loop)

tests/
  filesystem/           backends, tools, offload unit + end-to-end tests
  loop/                 agent loop tests
  tools/                tool registry, scheduler, reliability tests
  ...                   (one directory per subsystem)
examples/               runnable scripts (require OPENAI_API_KEY unless marked offline)
docs/                   this documentation
```

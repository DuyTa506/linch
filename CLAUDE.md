# CLAUDE.md

Guidance for Claude Code working in this repository. Keep it short; read the
linked docs when a task touches a specific subsystem.

## What this is

**Linch** is an async-first, event-driven, provider-agnostic Python SDK for
embedding a software-engineering agent loop inside other applications. Core
flow:

```
Agent (config) → Session (state) → run_loop() → Events → caller
```

Source lives in `src/linch/` (src-layout package). Tests mirror the source tree
under `tests/`; examples under `examples/`; docs under `docs/`.

## Commands

```bash
pip install -e '.[dev,mcp,anthropic,gemini]'   # dev install
pytest                                          # all tests
pytest tests/test_budget.py::test_name          # one test
ruff check . && ruff format --check .           # lint + format check
ruff check --fix . && ruff format .             # auto-fix
pyright                                          # type check
```

Run `pytest`, `ruff`, and `pyright` before opening a PR. Unit tests must not
depend on live services; live-provider coverage lives in `tests/integration/`
and skips cleanly without credentials.

## Where to read more (progressive disclosure)

Don't guess at a subsystem's internals — read its doc first. Two indexes map
the whole system:

- **`docs/architecture/README.md`** — per-subsystem contracts, data flow, and
  invariants (loop, providers, tools, permissions, memory, filesystem,
  compaction, skills/subagents, events, data types, module inventory).
- **`docs/usage/README.md`** — how to *use* each feature (agent config,
  providers, tools, context/memory, filesystem, hooks, structured output,
  deep agent, coordination, workflows, evals).
- **`docs/versioning.md`** — the semver / public-API contract.

When working on subsystem X, open the matching `docs/` page before editing, and
prefer the `file:line` references there over re-deriving behavior from scratch.

## Non-negotiable design constraints

These hold for *every* change, regardless of subsystem:

- **Async only** — no blocking I/O in the core loop (`loop/`, `scheduler.py`,
  `compaction.py`, `providers/`).
- **Tools are duck-typed protocols** — implement the protocol attributes
  directly; do not introduce a base class to inherit from.
- **`provider_view` vs `full_history`** are separate — only `provider_view` is
  sent to the LLM, and compaction mutates `provider_view` only.
- **The loop continues while a response has tool calls** and stops on a
  text-only response (or a stop condition).
- **Python 3.10+** — no 3.11+-only APIs (e.g. use `asyncio.wait_for` +
  `asyncio.TimeoutError`, not the unified `TimeoutError`/`asyncio.timeout`).
- **No vendor lock-in in core** — observability reaches Langfuse/LangSmith/etc.
  only through the OpenTelemetry seam; memory core ships no vector-DB or
  embedding dependency (such adapters live in `examples/`).
- **Multi-tenant safe** — no process-global mutable state; each `Agent` owns its
  own registries and session dict so N agents run concurrently in one process.
- **Opt-in features stay zero-overhead** — when a feature is unset (compaction
  ladder, verification gates, virtual filesystem, tool timeouts/retry) behavior
  must be byte-identical to before it existed.
- **Public API is exactly `linch.__all__`** — import from the top-level package;
  submodule paths and underscore names are private. `tests/test_public_api.py`
  guards this surface.

Style (imports, formatting, line length) is enforced by `ruff` — let the linter
handle it rather than hand-tuning. Match surrounding code; don't refactor
unrelated code in the same change.

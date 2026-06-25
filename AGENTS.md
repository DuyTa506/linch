# Repository Guidelines

## Project Structure & Module Organization

`linch` is a Python SDK packaged from `src/linch` (src-layout). Core runtime modules include `agent.py`, `session.py`, `scheduler.py`, `compaction.py`, `events.py`, and `types.py`; the agent loop is the `loop/` package (split by responsibility: `runner.py`, `streaming.py`, `request.py`, `terminals.py`, `finalize.py`). Feature packages include `providers/`, `tools/`, `context/`, `memory/`, `filesystem/`, `permissions/`, `sessions/`, `skills/`, `subagents/`, `coordination/`, `workflow/`, `deep_agent/`, `evals/`, `hooks/`, `observability/`, `loop_guard/`, and `mcp/`. Tests mirror these areas under `tests/`, with focused subdirectories such as `tests/providers/`, `tests/tools/`, `tests/storage/`, and `tests/integration/`. Runnable examples are grouped by topic under `examples/`, utility scripts live in `scripts/`, and docs live in `docs/` — start at `docs/architecture/README.md` (subsystem contracts and invariants) and `docs/usage/README.md` (how to use each feature), and read the relevant page before changing a subsystem.

## Build, Test, and Development Commands

Install for local development:

```bash
pip install -e '.[dev,mcp,anthropic,gemini]'
```

Run the main checks before opening a PR:

```bash
pytest
ruff check . && ruff format --check .
pyright
```

Use targeted tests while iterating, for example `pytest tests/tools/test_function_tools.py` or `pytest -k context`. Auto-fix style issues with `ruff check --fix . && ruff format .`. Live API tests require relevant credentials, such as `OPENAI_API_KEY`; unit tests must not depend on live services.

## Coding Style & Naming Conventions

Target Python 3.10+. Ruff enforces imports and lint rules (`E`, `F`, `I`, `UP`, `B`) with a 100-character line length. Use 4-space indentation, type annotations for public surfaces, and `slots=True` on new dataclasses following existing primitives. Keep runtime/provider paths async; avoid blocking I/O in `loop/`, `scheduler.py`, `compaction.py`, and provider modules. Tools are duck-typed protocols; do not introduce base-class inheritance where protocols are expected. The supported public surface is exactly `linch.__all__` (`tests/test_public_api.py` guards it); submodule paths and underscore names are private.

## Testing Guidelines

Tests use `pytest` with `pytest-asyncio` in auto mode. Name files `test_*.py` and keep assertions focused on one behavior. Prefer fake providers and `InMemorySessionStore` for loop tests. Live provider coverage belongs in integration tests and must skip cleanly without credentials. Some hardening tests reload `linch`; in affected tests, import `Agent`, `Session`, and related content types inside test functions rather than at module scope.

## Commit & Pull Request Guidelines

Recent history uses short imperative commit subjects, sometimes with a conventional prefix such as `chore:`. Examples: `Harden SDK from whole-codebase review` and `chore: exclude .claude and .codex from version control`. PRs should include a concise description, linked issue or motivation, behavioral notes, and the checks run. Add screenshots or logs only when they clarify UI, CLI, or observability changes.

## Security & Configuration Tips

Never commit `.env`, API keys, local caches, or generated private state. Keep provider-specific wire formats inside `src/linch/providers/`; shared loop code should consume normalized provider events only.

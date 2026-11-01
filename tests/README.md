# Test layout and support

Tests mirror the `src/linch` tree. Keep each topic in one directory and put
extension tests under `tests/ext/`; leave live integration tests in
`tests/integration/`. Examples and script checks live under `tests/examples/`
and `tests/scripts/`; package API checks live beside their package tests.

Shared deterministic fixtures live in `tests/support/`:

- `providers.py` provides scripted text/tool turns and records requests.
- `tools.py` provides a minimal duck-typed `StubTool` and registry builder.
- `agents.py` builds inert agents with in-memory stores and keyword overrides.
- `stores.py` provides in-memory factories and the shared `pg_dsn` fixture.

New tests should use these helpers instead of defining local fake providers or
generic agent builders. Keep behavior-specific doubles beside the test that
exercises them. Parametrize near-duplicates, keep one topic in one place, and
make Postgres cases self-skip when `AGENT_KIT_TEST_PG_DSN` or `asyncpg` is
unavailable. Imports use `from tests.support...` so bare `pytest` and
`python -m pytest` resolve the same helpers.

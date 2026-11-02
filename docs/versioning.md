# Versioning & public API

Use this page when choosing supported imports, planning a Linch upgrade, or
deciding whether persisted runs and databases can be reused.

Linch requires wire generation 3 for persisted state. Pre-3.0 and other
incompatible runs or databases are rejected; start fresh runs and use fresh
Postgres schemas. See [Migrating to Linch 3.0](migration-3.0.md) for migration
details.

## Public API

The supported Python API is exactly the names exported by `linch.__all__` and
their documented behavior. Import supported names from the top-level package:

```python
from linch import Agent, ModelCallEvent, RunBudget, ToolContext, tool
```

Importing `linch` alone does not load optional integrations. Extensions use
explicit `linch.ext.*` imports and are absent from `linch.__all__`. The optional
integrations below support imports from these exact modules; install their extra
before using them. Names exported by `linch.__all__` still use top-level
imports, including when an integration also exposes them. `PostgresRunStore`,
`__version__`, and `RUN_SCHEMA_VERSION` are root exports; the other Postgres
adapters stay on the
explicit module paths shown below.

| Supported module | Required extra | Integration |
|---|---|---|
| `linch.sessions.postgres` | `postgres` | Durable session store |
| `linch.filesystem.postgres` | `postgres` | Durable file backend |
| `linch.worker` | `worker` | Worker runtime, dispatch, and scheduling |

Names in `linch.__all__`, explicit `linch.ext.*` extensions, and the optional
modules above are supported surfaces. Other submodule paths and names are
private implementation details and may change without a public compatibility
guarantee.
`tests/package/test_public_api.py` checks that every declared export resolves, exports
are unique, and undeclared public attributes do not leak onto the package.

Requirements of public duck-typed protocols are part of the contract too.
Adding a required method, attribute, or argument that an existing host must
implement is an incompatible change. An optional capability probed at runtime
can be added compatibly when existing implementations retain their behavior.

## Stable releases after 3.0

After 3.0, Linch follows normal Semantic Versioning:

- **MAJOR**: incompatible public API or supported-behavior changes, including
  removed exports or parameters, incompatible protocol requirements, and
  changed defaults that break existing callers.
- **MINOR**: backward-compatible additions, including new public exports,
  optional parameters, and optional capabilities.
- **PATCH**: backward-compatible bug fixes and internal changes.

Public additions belong in MINOR releases; PATCH is not an exception for
additive API changes. Future stable releases must preserve compatibility within
a MAJOR.

## Dependencies and reproducible builds

Python 3.11 or newer is required. Optional extras install the adapters and
third-party SDKs needed for their integrations. Their supported dependency
ranges are declared in `pyproject.toml`; dependency changes are documented in
the changelog. A dependency or interpreter requirement change that breaks a
supported installation follows the stable compatibility policy above.

For a stable 3.x application, a range such as `linch>=3.0,<4.0` accepts
compatible releases. An exact Linch pin does
not pin transitive dependencies: use a lockfile for reproducible environments,
including extras. Review the changelog before updating the lock.

Build release wheels and source distributions from a clean export of the
intended commit's tracked files. Verify packaged sources and docs against that
commit and inspect artifacts for private workspace data before handoff.

## Persisted formats

Persisted schemas are versioned separately from the package version, but all
persisted envelopes share the private `WIRE_VERSION = 3` generation.
`linch.RUN_SCHEMA_VERSION` is the public value for that generation. Built-in
durable stores are Postgres-only: `PostgresRunStore` is a root export, while
`PostgresSessionStore` and `PostgresFileBackend` use the explicit module paths
in the table above. `InMemoryRunStore` and `InMemorySessionStore` are
process-local adapters for tests and development.

Checkpoints, run contracts, model-call intents/receipts and journals, exact
model-input snapshots, permission decisions, session/provider-view snapshots,
events, and workflow run metadata require the exact current version. Missing,
pre-3.0, and unknown versions are rejected; start a new run. Built-in Postgres
stores create complete version-3 schemas and reject incompatible existing
databases without upgrading them; use fresh databases for incompatible schemas.
Compatible version-3 schemas can receive the run-admission index during
initialization; duplicate nonterminal runs must be reconciled first. See
[Run stores](usage/run-stores.md).

Package versions are recorded for audit, not compared as resume identity.
Top-level tool descriptions are also excluded from the run fingerprint; tool
names, input/output schemas, scope, concurrency, and policy identities remain
significant. Drain all active runs before upgrading to 3.1.0 because the
Local/Docker backend policy versions changed to `2` and Bash schemas changed.
An unchanged wire generation alone does not make those contracts resumable.

Checkpoint wire fields are required, including nullable and extension-state
fields. A valid current version does not waive contract fingerprints,
accounting receipts, exact-input integrity checks, or recovery-cursor validation.
Malformed current-version provider-view caches may be rebuilt from full history;
an incompatible snapshot version fails rather than falling back.

Stored events are required on read. Unknown required event types and unknown
required discriminators, including `WorkflowEvent.kind`, raise errors rather
than being skipped or coerced into another kind. Only a valid unknown event
envelope explicitly declaring `ignorable: true` is preserved as an
`IgnorableEvent`; its opaque JSON is retained for round trips. Malformed
records and known event types mislabelled as ignorable are rejected.

Message decoding likewise rejects unknown content block types. Tool-result
blocks accept only `type`, `tool_use_id`, `content`, and `is_error`; other
tool-result fields are rejected rather than silently discarded. See
[Run stores](usage/run-stores.md) and [Events](usage/events.md) for operational
contracts.

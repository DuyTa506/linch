# Versioning & public API

Linch 3.0 is under development. During this transition, incompatible changes
may remove APIs, aliases, shims, and legacy persisted-data readers without a
warning or deprecation window. Compatibility with 2.x is not a requirement.
Pre-3.0 runs are rejected; start fresh runs and use fresh databases for 3.0.
See [Migrating to Linch 3.0](migration-3.0.md) for the removals.

## Public API

The supported Python API is exactly the names exported by `linch.__all__` and
their documented behavior. Import supported names from the top-level package:

```python
from linch import Agent, ModelCallEvent, RunBudget, ToolContext, tool
```

Submodule import paths and underscore-prefixed names are private implementation
details and may change without a public compatibility guarantee.
`tests/test_public_api.py` checks that every declared export resolves, exports
are unique, and undeclared public attributes do not leak onto the package.

Requirements of public duck-typed protocols are part of the contract too.
Adding a required method, attribute, or argument that an existing host must
implement is an incompatible change. An optional capability probed at runtime
can be added compatibly when existing implementations retain their behavior.

## Stable releases after 3.0

Once 3.0 stable ships, Linch follows normal Semantic Versioning:

- **MAJOR**: incompatible public API or supported-behavior changes, including
  removed exports or parameters, incompatible protocol requirements, and
  changed defaults that break existing callers.
- **MINOR**: backward-compatible additions, including new public exports,
  optional parameters, and optional capabilities.
- **PATCH**: backward-compatible bug fixes and internal changes.

Public additions belong in MINOR releases; PATCH is not an exception for
additive API changes. The unrestricted 3.0 transition above does not grant
future stable releases permission to break compatibility within a MAJOR.

## Dependencies and reproducible builds

Python 3.11 or newer is required. Optional extras install the adapters and
third-party SDKs needed for their integrations. Their supported dependency
ranges are declared in `pyproject.toml`; dependency changes are documented in
the changelog. A dependency or interpreter requirement change that breaks a
supported installation follows the stable compatibility policy above.

For a stable 3.x application, a range such as `linch>=3.0,<4.0` accepts
compatible releases after 3.0 stable is available. During prerelease
integration, pin the exact version you have reviewed; `3.0.0a1` remains the
current package version while development continues. An exact Linch pin does
not pin transitive dependencies: use a lockfile for reproducible environments,
including extras. Review the changelog before updating the lock.

Build release wheels and source distributions from a clean export of the
intended commit's tracked files. Verify packaged sources and docs against that
commit and inspect artifacts for private workspace data before handoff.

## Persisted formats

Persisted schemas are versioned separately from the package version, but all
persisted envelopes share the private `WIRE_VERSION = 3` generation.
`linch.RUN_SCHEMA_VERSION` reflects that generation. Checkpoints, run contracts,
model-call intents/receipts and journals, exact model-input snapshots, permission
decisions, session/provider-view snapshots, events, and workflow run metadata
require the exact current version. Missing, pre-3.0, and unknown versions are
rejected; start a new run. Built-in SQLite and Postgres stores create complete
version-3 schemas and reject incompatible existing databases without upgrading
them; use fresh databases.

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
blocks accept only `type`, `tool_use_id`, `content`, and `is_error`; removed
discovery controls are rejected rather than silently discarded. See
[Run stores](usage/run-stores.md) and [Events](usage/events.md) for operational
contracts.

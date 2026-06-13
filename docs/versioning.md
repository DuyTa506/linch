# Versioning & public API

Linch follows [Semantic Versioning](https://semver.org/). This page is the contract
an embedder can pin to.

## What is public

The supported public API is exactly the names exported from the top-level package —
`linch.__all__`. Import from the top level:

```python
from linch import Agent, RunBudget, tool   # supported
```

Everything else is an implementation detail:

- **Submodule paths** (`linch.loop`, `linch.tools.execution`, `linch.permissions.engine`, …)
  are *not* part of the contract. They may move or change between minor versions. A handful
  are re-exported through `linch.__all__` (e.g. provider classes); use those, not the path.
- **Underscore-prefixed modules and names** (`linch._version`, `agent._attach_mcp_tools`,
  `storage._executor`, …) are private and may change at any time.

`tests/test_public_api.py` locks this: every `__all__` name must resolve, there are no
duplicate entries, and no public (non-underscore) attribute leaks onto the package without
being declared. Changing the public surface is therefore a deliberate edit, reviewable in
the diff.

## What the version number means

Given `MAJOR.MINOR.PATCH`:

- **MAJOR** — a breaking change to a name in `linch.__all__`: a removed/renamed export, a
  removed parameter, a changed default that alters behavior, or a protocol method signature
  change. Removing a feature flag's *opt-in* default counts (the loop stops being
  byte-identical for an existing caller).
- **MINOR** — additive, backward-compatible: a new export, a new optional `Agent(...)`
  parameter that defaults to today's behavior, a new event type, a new opt-in seam. Existing
  code keeps working unchanged. Every feature added in the roadmap is minor-compatible by
  construction — defaults leave the loop byte-identical.
- **PATCH** — bug fixes and internal changes with no public-surface effect.

A duck-typed **protocol** (`Tool`, `MemoryStore`, `RunObserver`, `FileBackend`,
`ExecutionBackend`, `Mailbox`, `IsolationBackend`, `ScheduleStore`, `Verifier`, …) is part
of the contract: adding a *required* method or argument an embedder must implement is a
MAJOR change. Adding an *optional* one the runtime probes with `getattr`/`hasattr` is MINOR.

## Wire formats are versioned separately

Persisted formats carry their own integer version so a stored run survives a library
upgrade:

- `linch.RUN_SCHEMA_VERSION` stamps every serialized `RunCheckpoint`
  (`checkpoint_to_dict` → `"schema_version"`). A checkpoint written by a newer binary
  round-trips its known fields on an older one; `load_events` skips an event row it cannot
  decode rather than aborting the resume. See [usage/workflows.md](usage/workflows.md) and
  the run-store source for details.

A breaking wire-format change bumps `RUN_SCHEMA_VERSION`, independent of the package
MAJOR/MINOR/PATCH.

## Deprecation policy

A public name slated for removal is kept for at least one MINOR release with a
`DeprecationWarning` before it is dropped in a MAJOR release. Deprecated aliases that already
exist (`defaultTools` → `default_tools`, `tools_from_defaults`) follow this rule.

## Pinning

```toml
# pyproject.toml — pin to a compatible range
dependencies = ["linch>=1.0,<2.0"]
```

Pin the MAJOR if you depend only on `linch.__all__`. Pin MINOR as well if you implement a
protocol and want to review new optional methods before adopting them.

# Versioning & public API

Linch versions `MAJOR.MINOR.PATCH` and never breaks `linch.__all__` outside a MAJOR.
This page is the contract an embedder can pin to.

It deviates from strict [Semantic Versioning](https://semver.org/) in two ways, both
deliberate and both spelled out below: **purely additive public surface can ship in a
PATCH**, and **an optional extra's dependency floor is not covered by the package version
at all**. If either matters to you, pin exactly — see [Pinning](#pinning).

## What is public

The supported public API is exactly the names exported from the top-level package —
`linch.__all__`. Import from the top level:

```python
from linch import (
    Agent,
    EvalCase,
    RunBudget,
    ScriptedProvider,
    ToolRule,
    run_eval,
    tool,
)
```

Evaluation harness types/scorers and permission rules are part of that same
top-level contract; callers do not need to depend on the `linch.evals` or
`linch.permissions` module paths.

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
- **MINOR** — a backward-compatible release large enough to be worth announcing: a feature
  set, a new subsystem, a notable group of additions. Existing code keeps working unchanged.
  Every feature added in the roadmap is compatible by construction — defaults leave the loop
  byte-identical.
- **PATCH** — bug fixes, internal changes, and *purely additive* public surface: a new
  export, a new optional `Agent(...)` parameter that defaults to today's behavior, a new
  event type, a new opt-in seam. Existing code cannot observe any of these without asking
  for them.

The MINOR/PATCH line is therefore an **editorial** judgment about the size of a release, not
a compatibility signal — both are equally safe to take. Strict SemVer would put every
addition in MINOR; Linch does not, so **do not read "PATCH" as "nothing was added."** The
compatibility guarantee lives entirely at the MAJOR boundary. Read `CHANGELOG.md` before
upgrading, whichever digit moved.

A duck-typed **protocol** (`Tool`, `MemoryStore`, `RunObserver`, `FileBackend`,
`ExecutionBackend`, `Mailbox`, `IsolationBackend`, `ScheduleStore`, `Verifier`, …) is part
of the contract: adding a *required* method or argument an embedder must implement is a
MAJOR change. Adding an *optional* one the runtime probes with `getattr`/`hasattr` is MINOR.

## Optional-extra dependencies are not covered

The version contract above describes `linch.__all__`. It says nothing about what an
**extra** (`linch[mcp]`, `linch[anthropic]`, `linch[gemini]`, `linch[otel]`,
`linch[postgres]`) requires underneath. A third-party SDK can make a breaking release at any
time, and keeping an adapter working may mean raising its floor — including in a PATCH.

Precedent: **1.2.1 raised `linch[mcp]` to `mcp>=2.0.0` and dropped support for mcp 1.x**,
because mcp 2.0 renamed the streamable-HTTP transport, replaced its `headers=` argument with
a caller-supplied httpx client, and moved its models to snake_case. No name in
`linch.__all__` changed, so by the rules above it was not a MAJOR — but an environment
pinned to mcp 1.x will fail to resolve.

Linch's own config surface is held stable across such a bump wherever possible
(`McpServerConfig.headers` kept working unchanged), and every floor change is called out at
the top of its `CHANGELOG.md` entry. If you cannot absorb one, pin `linch` exactly and
upgrade deliberately.

## Wire formats are versioned separately

Persisted formats carry their own integer version so a stored run survives a library
upgrade:

- `linch.RUN_SCHEMA_VERSION` stamps every serialized `RunCheckpoint`
  (`checkpoint_to_dict` → `"schema_version"`). A checkpoint written by a newer binary
  round-trips its known fields on an older one; `load_events` skips an event row it cannot
  decode rather than aborting the resume. See [usage/workflows.md](usage/workflows.md) and
  the run-store source for details.
- **New `WorkflowEvent` kinds are additive and do not bump `RUN_SCHEMA_VERSION`.** An older
  binary reading a newer run's event log decodes an unknown kind as `phase`
  (`WORKFLOW_EVENT_KINDS` in `events.py`), which the journal fold ignores — so the affected
  call simply re-executes on resume instead of replaying. That is fail-safe, not a wrong
  answer, so it stays a MINOR change.
- **`RunStatus` / `RunPhase` gained `"suspended"` / `"workflow_suspended"`** for a workflow
  parked at a `wf.interrupt`. Neither `Literal` is public API, the status column has no
  constraint, and `checkpoint_from_dict` does not validate `phase`, so an older binary
  reads such a run without error.
- **`RunCheckpoint.extension_state` namespaces are opaque to core.** The workflow journal
  snapshot lives under `"linch.workflow"`. A reader that does not understand it preserves
  it; a reader that does treats a missing, malformed, or stale entry as "no snapshot" and
  folds the whole event log. So it never needs a version of its own.

A breaking wire-format change bumps `RUN_SCHEMA_VERSION`, independent of the package
MAJOR/MINOR/PATCH.

## Deprecation policy

A public name slated for removal is kept for at least one MINOR release with a
`DeprecationWarning` before it is dropped in a MAJOR release. Deprecated aliases that already
exist (`defaultTools` → `default_tools`, `tools_from_defaults`) follow this rule.

## Pinning

```toml
# pyproject.toml — no name in `linch.__all__` will break inside this range
dependencies = ["linch>=1.0,<2.0"]
```

That range is the *compatibility* guarantee, and it is enough if you only call
`linch.__all__` and can take a new export or a raised extra floor without noticing. It is
**not** a "nothing will change" guarantee: a PATCH inside it may add a public name, and a
PATCH may raise what `linch[...]` requires.

Pin tighter when you need the release to be inert:

```toml
# review every release before taking it
dependencies = ["linch==1.2.1"]
```

- **Implementing a protocol?** Pin the exact version, or at least the MINOR. New optional
  methods the runtime probes with `getattr` can arrive in any release, and you will want to
  read the changelog before adopting them.
- **Using an extra with a pinned third-party SDK?** Pin the exact version — see
  [Optional-extra dependencies](#optional-extra-dependencies-are-not-covered).
- **Locking a build?** Use a lockfile (`uv.lock`, `poetry.lock`, `pip-compile`). It pins the
  extras' transitive dependencies too, which no `linch` specifier can do.

# Development Principles

> Part of the [Linch architecture guide](./README.md).

Linch is an async-first SDK. Keep the core small, provider-neutral, and safe to
embed alongside other agents in one process. These principles are the review
checklist for new extension points and runtime changes.

## Scope and ownership

- Every `Agent` owns one per-agent `Context`; shared services are scoped there,
  never stored in mutable process globals. A supplied context is also disposed
  by that Agent; pass a child scope when a host must retain the parent.
- A `Session` owns one append-only `SessionLog`. Its `provider_view` and
  `full_history` are deep-copied, read-only projections; per-request context is
  ephemeral and remains outside the log.
- Durable persistence ends at the session snapshot boundary. In-memory log
  entries and projection bookkeeping are not a second durable journal.

## Extension and effects

- Prefer a capability seam or kernel listener over a branch in the loop.
- Registrations are reversible: `register`/`on` return an identity-checked
  `Disposable`. Await disposal; teardown is single-use after success and
  retryable after failure.
- Middleware `next` handles are single-use. A wrapper must await `next()` once
  to delegate, or deliberately short-circuit without calling it.
- Optional paths preserve the direct path when unused, including the tool
  pipeline and observability hooks.

## Compatibility and durability

- Required persisted fields are strict on read. Only producer-declared
  `ignorable` events may be represented by `IgnorableEvent` and skipped.
- Durable resume fingerprints all policy-bearing execution semantics, including
  the effective shell+filesystem `ExecutionBackend` and active tool pipeline.
  Opaque custom providers fail closed instead of guessing identity.
- Pipeline listeners run after permission and cannot rewrite authorization-
  sensitive input; use the pre-tool hook for a transform that is revalidated.
- Keep compatibility shims at the boundary, document deprecations, and provide
  an explicit migration path. A shell-only execution backend is legacy; new
  integrations provide a coherent `.shell` and `.fs` world.

## Async and security

- Keep runtime/provider/filesystem paths async; no blocking I/O in the turn loop.
- Permissions decide whether an approved operation may run; capability backends
  decide where it runs. Do not infer sandbox confinement from an opaque backend.
- Preserve duck-typed protocols and the supported `linch.__all__` surface; add
  public names deliberately and test their import and serialization contracts.

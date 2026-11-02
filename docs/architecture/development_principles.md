# Development Principles

> Part of the [Linch architecture guide](./README.md).

Use this page when reviewing a kernel extension, provider adapter, durable-state
change, or other runtime contribution.

Linch is an async-first SDK. Keep the core small, provider-neutral, and safe to
embed alongside other agents in one process. These principles are the review
checklist for new extension points and runtime changes.

## Scope and ownership

- Every `Agent` owns one per-agent `Context`; when a host context is supplied,
  that context remains the host-owned parent and the Agent owns a child scope.
  Shared services stay scoped there, never in mutable process globals; pass an
  explicit child scope when the host wants its own boundary.
- A `Session` owns one append-only `SessionLog`. Its `provider_view` and
  `full_history` are deep-copied, read-only projections; per-request context is
  ephemeral and remains outside the log.
- `SessionLog` entries and projection bookkeeping are in-memory only. Durable
  session messages and optional provider-view snapshots belong to the session
  store; the in-memory log is not a second durable journal.

## Extension and effects

- Prefer a capability seam or kernel listener over a branch in the loop.
- Registrations are reversible: `register`/`on` return an identity-checked
  `Disposable`. Await disposal; teardown is single-use after success and
  retryable after failure.
- Middleware `next` handles are single-use. A wrapper must await `next()` once
  to delegate, or deliberately short-circuit without calling it.
- Optional paths preserve the direct path when unused, including the tool
  pipeline and hook dispatch.

## Compatibility and durability

- Required persisted fields are strict on read. Only producer-declared
  `ignorable` events may be represented by `IgnorableEvent` and skipped.
- Durable resume fingerprints all policy-bearing execution semantics, including
  the effective shell+filesystem `ExecutionBackend` and active tool pipeline.
  Opaque custom providers fail closed instead of guessing identity.
- Pipeline listeners run after permission and cannot rewrite authorization-
  sensitive input; use the pre-tool hook for a transform that is revalidated.
- Persisted envelopes require the exact current wire/schema version; missing,
  pre-3.0, and unknown versions are rejected, so start a new run and use a fresh
  database when the version gate fails. Execution backends must provide one
  coherent `.shell` and `.fs` world; shell-only values are rejected.

## Async and security

- Keep runtime/provider/filesystem paths async; no blocking I/O in the turn loop.
- Permissions decide whether an approved operation may run; capability backends
  decide where it runs. Do not infer sandbox confinement from an opaque backend.
- Preserve duck-typed protocols and the supported `linch.__all__` surface; add
  public names deliberately and test their import and serialization contracts.

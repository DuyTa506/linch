# Exact provider wire-prefix observability implementation plan

Planning-only execution plan for the independent Python-native Linch SDK.
Spec: `../specs/2026-10-30-prefix-observability.md`

## Global Constraints

These constraints are binding for every task:

- Preserve the observational boundary: no cache-hit/miss claim, prompt change,
  request freeze, prefix rewrite, provider retry, rejection, or cache-control
  change caused by this feature. Existing capability lowering may still
  sanitize fields that a hook adds after a provider declared them unsupported.
- The provider adapter is the only authority for the exact final wire surface.
  Core must not reconstruct a descriptor from ProviderRequest, old tool
  signatures, prompts, or generic serialization.
- A provider without an exact synchronous hook, or whose hook returns None,
  raises, or cannot represent hidden SDK/server behavior, gets no wire
  advisory. Never emit an approximate surrogate.
- Descriptor data is detached, bounded, canonical JSON plus component digests.
  Raw prompts, message bodies, tool schemas/descriptions, credentials, headers,
  URLs, payloads, descriptors, and fingerprints never enter events, reports,
  logs, checkpoints, observer arguments, or persistent session state.
- The primary Session prefix baseline is updated only by primary physical
  provider attempts in the main turn stream. Auxiliary compaction/provider
  calls must not invoke the primary observation callback or overwrite that
  baseline. Their usage, receipts, and accounting events remain owned by the
  separate model-call accounting ledger.
- Keep existing model_changed and tool_set_changed advisories and their wire
  shape. Add wire_prefix_changed through the event codec as an additive
  reason; do not postpone its codec until after a task that emits it.
- Optional methods remain duck-typed and backward compatible for providers that
  omit them. Do not add a required public abstract method or a new dependency.
- No network/live-provider tests. No worker stages, commits, tags, publishes,
  or calls a release service. Every task ends with focused checks and an
  independent review gate before the next task begins.

### Task 1: Core descriptor, canonicalization, and event contract

**Files:**

- Modify: src/linch/_prompt_cache.py
- Modify: src/linch/providers/base.py
- Modify: src/linch/events.py
- Modify: src/linch/reports.py only if a dedicated wire-prefix summary is
  necessary; the generic sorted advisory-reason map is preferred
- Create: tests/test_wire_prefix_advisory.py
- Modify: tests/test_prompt_cache_advisory.py
- Modify: tests/providers/test_provider_capabilities.py

**Interfaces:**

- Optional synchronous BaseProvider.cache_prefix_descriptor(req) returning a
  detached descriptor or None; the base default remains None.
- Strict descriptor normalizer, component digest, versioned overall
  fingerprint, and deterministic redacted changed-part helpers in the private
  prompt-cache module.
- PromptCacheAdvisoryEvent accepts wire_prefix_changed while retaining the
  existing serialized fields and legacy reasons.

**Steps:**

- [ ] Define and validate the exact linch.wire-prefix.v1 descriptor schema:
  bounded labels, provider/model identity match, ordered unique parts, and
  sha256:<64 lowercase hex> component digests.
- [ ] Implement strict canonical JSON (UTF-8, compact separators,
  ensure_ascii=False, allow_nan=False, sorted object keys, preserved array
  order) and domain-separated component/overall SHA-256 helpers. Reject
  cycles, unknown keys, non-JSON values, duplicate parts, bad labels, and bad
  digests without coercion.
- [ ] Add the optional BaseProvider hook without changing provider
  conformance or the public root API. Document in code that the hook is
  synchronous, pure, no-I/O, and must use the adapter's own final payload
  builder.
- [ ] Add wire_prefix_changed to the event discriminator, encoder, decoder,
  and report counting path. Keep old event fields and model/tool reason
  behavior byte-compatible; verify strict old-reason validation remains.
- [ ] Test canonical key-order stability, meaningful parts-order drift,
  redacted component diffs, event round-trip, report folding, invalid
  descriptors, and a fake opt-in provider's stable fingerprint. Include a
  secret-shaped invalid descriptor and assert no secret enters any event or
  diff detail.
- [ ] Run the focused core/event/capability tests and formatting/lint checks
  for touched files.
- [ ] Review gate: independent reviewer checks descriptor exactness, event
  codec compatibility, secret non-disclosure, and no cache-hit semantics.
  Resolve findings and record the verdict before Task 2.

### Task 2: Primary final-request observation and session baseline

**Files:**

- Modify: src/linch/loop/request.py
- Modify: src/linch/loop/runner.py
- Modify: src/linch/loop/streaming.py
- Modify: src/linch/session.py
- Modify: tests/test_wire_prefix_advisory.py
- Modify only focused hook/capability/compaction-ladder tests required by the
  new primary-attempt callback

**Interfaces:**

- Private primary-only observation callback, conceptually
  observe_primary_provider_attempt(req, turn_index) -> list[Event], called
  after preparation and before the matching provider-start lifecycle hook and
  provider.stream().
- Private Session fields for the detached last primary descriptor and
  fingerprint; fields are ephemeral and never serialized.
- Streaming attempt loops yield callback events before each primary physical
  stream. Auxiliary compaction/model-call paths do not use this callback.

**Steps:**

- [ ] Reapply the existing provider capability downgrade after a mutable
  BeforeProviderCall result on normal requests; preserve exact frozen request
  replay by skipping re-lowering of the stored snapshot.
- [ ] Add the two private Session baseline fields with None defaults. Do not
  add them to RunContract, checkpoints, extension state, session history,
  exact model-input snapshots, or accounting receipts.
- [ ] Wire the callback once per primary physical attempt, including
  same-model retry, fallback-model, and context-retry paths. Yield any
  redacted event before provider streaming; equal retries do not duplicate an
  event and a changed fallback can emit one.
- [ ] Invoke the adapter hook only on the final primary request. Validate and
  deep-copy its descriptor; on None, malformed data, or an exception, clear
  the primary baseline, debug-log, and dispatch the unchanged request.
- [ ] Compare only consecutive valid primary observations in one Session:
  first/equal/no-baseline emits nothing; changed emits exactly one
  wire_prefix_changed detail containing labels only; then replace the
  baseline. A process restart or unavailable gap starts fresh.
- [ ] Add an explicit auxiliary isolation fixture: an accounted compaction
  call may produce a separate usage/receipt event, but it must not invoke the
  primary callback, emit a primary wire advisory, or overwrite the Session
  baseline. Do not edit the accounting ledger in this slice.
- [ ] Test hook-final mutation, final capability lowering, first/equal/changed
  observations, malformed/raising hooks, unsupported gaps, frozen replay,
  same-model retry, fallback-model retry, and context retry. Assert the
  request passed to stream is unchanged by observation.
- [ ] Run focused loop, prompt-cache, capability, and compaction-ladder tests.
- [ ] Review gate: independent reviewer verifies primary-only scope, callback
  ordering, frozen replay behavior, auxiliary/accounting isolation, and no
  duplicate retry events. Fix and re-review before Task 3.

### Task 3: Exact built-in provider adapters and security acceptance

**Files:**

- Modify: src/linch/providers/openai_chat.py
- Modify: src/linch/providers/openai_responses.py
- Modify: src/linch/providers/anthropic.py
- Modify: tests/providers/test_openai_chat_stream.py
- Modify: tests/providers/test_openai_responses.py
- Modify: tests/providers/test_anthropic_provider.py
- Modify: tests/test_wire_prefix_advisory.py for inherited OpenAI-compatible
  family coverage

**Interfaces:**

- OpenAI-compatible family hook derives parts from the dynamic _build_payload
  used by stream, including provider-specific extra_body/cache settings.
- Responses hook derives from build_payload and includes the exact
  previous_response_id/input-chain boundary when present.
- Anthropic hook derives from _build_payload after native/compatible mode,
  translated system cache markers, tools, and cache controls are resolved.
- Gemini and opaque/custom SDKs remain None until their final wire surface is
  provable; no generic fallback serializer is introduced.

**Steps:**

- [ ] Implement the shared OpenAI-compatible descriptor projection so
  OpenAI Chat, vLLM, SGLang, DeepSeek, and llama.cpp use their actual dynamic
  payload builder rather than a duplicate request translator.
- [ ] Implement Responses and Anthropic projections from the exact builders,
  with adapter-specific cache/chain parts and no client kwargs, credentials, or
  transport metadata.
- [ ] Capture final payload projections in network-free provider tests and
  independently compute expected component digests. Assert the provider hook
  and stream payload agree for static system, transformed tool order/schema,
  cache settings, and Responses chaining.
- [ ] Add secret fixtures containing API keys, authorization/cookie headers,
  URLs, prompt/schema text, and secret-like descriptor keys. Assert none
  appear in normalized output when rejected, event detail/JSON, reports,
  timeline, logs, or observer arguments.
- [ ] Add unsupported-adapter tests proving no wire advisory is emitted and
  existing model/tool advisories remain the only fallback diagnostics.
- [ ] Run focused provider payload/conformance, wire-prefix, and event tests;
  run Ruff format/check on touched files.
- [ ] Review gate: independent reviewer compares each descriptor against the
  real final adapter builder, verifies primary-only invocation, secret
  boundaries, and no approximate surrogate. Fix and re-review before Task 4.

### Task 4: Documentation, version, and prerelease preparation

**Files:**

- Modify: docs/architecture/provider-contract.md
- Modify: docs/architecture/events.md
- Modify: docs/usage/providers.md
- Modify: docs/versioning.md
- Modify: CHANGELOG.md
- Modify: pyproject.toml only after the root release owner approves the exact
  3.0 prerelease version string

**Interfaces:**

- Provider docs specify the optional exact hook, descriptor redaction,
  primary-only baseline, None/no-surrogate behavior, and adapter rollout.
- Event docs specify wire_prefix_changed, strict reader compatibility, and
  that provider cache usage—not the advisory—is evidence of a hit.
- Version/changelog metadata records the optional feature and 3.0 prerelease
  target without changing 2.x defaults or public API accidentally.

**Steps:**

- [ ] Update provider/event/usage docs to match the implemented hook,
  canonical descriptor, redacted detail, primary-only lifecycle, auxiliary
  accounting separation, and unsupported-adapter caveat.
- [ ] Add a concise CHANGELOG entry describing the additive event reason,
  opt-in provider support, no cache-hit claim, and strict event-reader upgrade
  note.
- [ ] Check docs/versioning semver and wire-format guidance. Only the root
  release owner chooses and authorizes a 3.0 prerelease version; if approved,
  update pyproject.toml and verify linch.__version__ fallback. Do not bump
  versions speculatively.
- [ ] Run the full pytest suite, Ruff check, Ruff format --check, Pyright,
  build sdist/wheel, inspect wheel metadata, and run an isolated installed
  smoke test for event decoding and provider hook imports.
- [ ] Review gate: independent final reviewer checks docs/spec/code/test
  agreement, version/changelog accuracy, wheel contents, and unresolved
  compatibility notes.
- [ ] Handoff to root with paths, focused/full check outputs, review verdicts,
  unsupported adapter list, and any release blocker. No worker may git tag,
  publish to PyPI, upload artifacts, create a GitHub release, or claim remote
  publication.

## Final handoff record

- [ ] Root records each task's review verdict and exact test output in the
  integration ledger.
- [ ] Root decides whether the prerelease metadata is staged for a later
  release commit; this plan does not authorize publication.

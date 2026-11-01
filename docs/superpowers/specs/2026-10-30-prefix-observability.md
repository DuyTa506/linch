# Exact provider wire-prefix observability

Status: implementation specification. This is a Linch-native, optional,
observational slice. It is not a cache implementation and it is not a claim
that a provider returned a cache hit.

## 1. Scope and non-negotiable behavior

The current prompt-cache advisory compares only the model and Linch's
pre-adapter tool signature. That remains in place. This slice adds an optional
provider-adapter hook that can describe the exact cache-relevant wire prefix
after the final request hooks and capability downgrades have run.

The behavior is:

1. A provider may opt in with a synchronous, deterministic adapter hook. The
   hook receives the exact `ProviderRequest` object that will be passed to
   `provider.stream()` and returns a validated, non-secret descriptor or
   `None`.
2. The descriptor is produced from the adapter's own final wire translation,
   not from a generic `ProviderRequest` surrogate. It contains only bounded
   labels and SHA-256 component digests; prompts, message bodies, tool
   descriptions/schemas, credentials, headers, URLs, and raw payloads never
   leave the adapter.
3. Core canonicalizes the descriptor with stable object-key ordering and
   order-preserving arrays, then computes a versioned SHA-256 fingerprint. A
   descriptor is compared only with the previous valid descriptor held
   ephemerally on the same `Session`.
4. A change emits an additive `PromptCacheAdvisoryEvent` with reason
   `wire_prefix_changed` and a redacted component-only detail. It does not
   reject, rewrite, freeze, retry, or delay a request. Legitimate drift is
   allowed.
5. The feature never emits a cache-hit or cache-miss assertion. Actual
   provider usage (`Usage.cache_read_tokens`) remains the only cache evidence.
6. A provider without the hook, a hook returning `None`, a hook that cannot
   determine an exact prefix, or an invalid/raising hook produces no new wire
   advisory. Core must not fall back to the old tool signature as an
   approximate wire fingerprint.

No OpenHuman/Tiny* code, dependency, or wire format is copied. The
implementation uses Python standard-library hashing/JSON plus existing Linch
types and is independent of OpenHuman's GPL license.

## 2. Final-request boundary

The observed request is the request at the final pre-dispatch boundary.

The descriptor is computed once for every primary physical provider attempt,
not only once per turn. This includes same-model retries, fallback-model
attempts, and context-retry paths in the primary turn stream. A fallback that
changes the request model therefore gets its own observation; an unchanged
retry compares equal and emits nothing. The observation callback yields any
advisory event before the corresponding provider stream is started:

```text
_build_turn_request()
  -> initial provider capability downgrade
  -> BeforeProviderCall hooks (may replace/mutate ProviderRequest)
  -> final capability downgrade on the hook result
  -> optional exact model-input snapshot
  -> cache_prefix_descriptor(req)
  -> provider.stream(req)
```

`loop/request.py` already applies capability downgrades while assembling a
request. The runner must apply the same downgrade once more after a mutable
`BeforeProviderCall` result, so a hook cannot make the descriptor describe a
natively unsupported field that the adapter will later remove. The final
downgrade is skipped for an exact frozen replay whose stored request is
explicitly the wire input to preserve snapshot semantics; the descriptor then
observes that stored request.

The hook is called immediately before the provider attempt, after the request
has passed `_dispatch_before_provider_call`. It is synchronous and must not
do network I/O, mutate `req`, inspect process-global secrets, or consult a
provider response. The provider's `stream()` method continues to emit the
existing normalized event contract. A provider may share its pure payload
builder between `stream()` and the descriptor hook; it must not maintain two
independent translations that can drift.

The descriptor is computed before the primary stream is advanced. Auxiliary
compaction/provider calls are outside this observation path: their accounting
receipts and usage events remain owned by the model-call ledger and must not
overwrite the Session's primary prefix baseline. A descriptor failure is
handled as an unavailable observation and logged at debug level; it never turns
a successful provider call into a failed run. The current request is still
sent unchanged.

## 3. Adapter hook and descriptor contract

Add this optional duck-typed method to the provider contract. It is a default
`None` method on `BaseProvider` so existing providers and test fakes remain
valid:

```python
def cache_prefix_descriptor(
    self, req: ProviderRequest
) -> dict[str, JsonValue] | None:
    """Return an exact, redacted wire-prefix descriptor, or None."""
```

The return value is a detached JSON object with exactly this shape:

```json
{
  "schema": "linch.wire-prefix.v1",
  "adapter": "openai-compatible.v1",
  "provider_id": "openai-chat",
  "model": "gpt-5",
  "parts": [
    {"name": "cache_controls", "digest": "sha256:<64 lowercase hex>"},
    {"name": "tools", "digest": "sha256:<64 lowercase hex>"},
    {"name": "system", "digest": "sha256:<64 lowercase hex>"},
    {"name": "chain.previous_response", "digest": "sha256:<64 lowercase hex>"}
  ]
}
```

`parts` is ordered in the provider's cache-prefix order. It may be empty
when the adapter has an exact empty prefix. Omit a part only when that wire
segment is absent; do not use an empty digest as an absence sentinel. Part
names are ASCII labels matching `[a-z0-9][a-z0-9_.-]{0,63}` and must be
unique. `schema`, `adapter`, `provider_id`, and `model` are bounded
non-secret labels; the descriptor validator requires
`provider_id == provider.id` and `model == req.model`.

Each component digest is calculated by the adapter over the exact semantic
wire segment it claims to represent:

```text
sha256(b"linch.wire-part.v1\\0" + strict_canonical_json(wire_segment))
```

The adapter may include a whole transformed segment, a cache-boundary marker,
or a provider-specific chain boundary. It must include all cache-relevant
settings it knows affect that prefix, for example an explicit cache-control
placement, TTL, `cache_prompt`/cache-report setting, or a Responses
`previous_response_id` chain boundary. It must not include a raw value in the
descriptor merely because that value is convenient to serialize. A digest is
the representation for message text, schemas, identifiers, and any other
possibly sensitive wire content.

The descriptor's `parts` list is the exact adapter-defined prefix surface,
not an assertion that every request byte is cacheable. If an adapter cannot
state which translated fields form its cache-relevant prefix (including a
provider SDK's hidden serialization or server-side state), it returns
`None`. The core never reconstructs a prefix from `req.system`,
`req.tools`, `req.messages`, or the old `tool_signature()` helper.

## 4. Canonicalization and fingerprint

Implement private helpers in `src/linch/_prompt_cache.py` (or a small private
module imported only there and by provider adapters):

```python
def wire_component_digest(wire_segment: JsonValue) -> str: ...

def normalize_wire_prefix_descriptor(
    raw: Mapping[str, object], *, provider_id: str, model: str
) -> dict[str, JsonValue]: ...

def wire_prefix_fingerprint(
    descriptor: Mapping[str, JsonValue]
) -> str: ...

def wire_prefix_changed_parts(
    previous: Mapping[str, JsonValue],
    current: Mapping[str, JsonValue],
) -> tuple[str, ...]: ...
```

Use the existing strict JSON rules as the model: reject non-string mapping
keys, cycles, NaN/Infinity, non-JSON objects, unknown top-level descriptor
keys, duplicate part names, malformed labels, and malformed digest strings.
Canonical JSON uses UTF-8, `ensure_ascii=False`, `allow_nan=False`,
`sort_keys=True`, and compact separators. Object key ordering is therefore
stable regardless of insertion order; array ordering, especially `parts`, is
preserved because wire order is meaningful.

The overall fingerprint is:

```text
sha256(b"linch.wire-prefix.v1\\0" + strict_canonical_json(normalized_descriptor))
```

Return a `sha256:<64 lowercase hex>` string. Normalize and deep-copy the
descriptor before storing it on a session so an adapter cannot mutate a prior
baseline after comparison.

`wire_prefix_changed_parts()` reports only safe labels, in deterministic
previous-then-current order with duplicates removed. A changed top-level
`adapter`, `provider_id`, or `model` contributes that label. A changed
part digest contributes its part name; a pure `parts` reorder contributes
`order`. It never returns descriptor values or raw digests.

## 5. Session lifecycle and event behavior

Add private ephemeral fields to `Session`:

```python
_last_wire_prefix_descriptor: dict[str, JsonValue] | None = None
_last_wire_prefix_fingerprint: str | None = None
```

They are not part of `RunContract`, `RunCheckpoint`,
`extension_state`, session history, or exact model-input snapshots. On a
process restart there is no baseline and therefore no wire advisory on the
first supported call. If a provider returns `None` or the hook cannot be
validated, clear the wire baseline rather than comparing across an unknown
gap. A later valid descriptor starts a fresh baseline without an event.

The baseline is updated after each valid primary physical attempt, not only
after a successful assistant assembly. A failed or cancelled primary attempt
with a valid descriptor still establishes the descriptor that was about to be
sent; an unavailable observation clears the baseline. Auxiliary compaction
attempts never update this baseline. Their accounting remains separate. This
is diagnostic state only and does not imply provider billing or cache success.

For a valid descriptor from a primary attempt:

1. No prior valid descriptor: save the normalized descriptor/fingerprint and
   emit nothing.
2. Equal fingerprint: update the ephemeral baseline and emit nothing.
3. Different fingerprint: emit exactly one
   `PromptCacheAdvisoryEvent(reason="wire_prefix_changed", detail=...)`,
   then replace the baseline.

The existing `model_changed` and `tool_set_changed` advisories continue to
run with their current semantics. A wire advisory is additive, so one request
may legitimately produce an old model/tool advisory and the exact wire
advisory. The new detail is fixed-format and redacted, for example:

```text
wire_prefix_changed; components=cache_controls,tools,system; values=redacted
```

The detail may contain only the safe component labels and the literal markers
`wire_prefix_changed`, `components`, `values`, `redacted`, and `order`.
It must not contain a prompt, tool description/schema, request body,
credential, header, URL, descriptor JSON, fingerprint, or provider error
text. The event is emitted before provider streaming and is otherwise an
ordinary event: `event_to_dict`/`event_from_dict`, run-store journaling,
reports, and `RunTelemetryHook`/OTel event forwarding continue to handle it
through the existing event path. No provider span attribute is required.

Add `wire_prefix_changed` to the advisory reason discriminator and codec.
The existing two reasons and serialized fields remain unchanged. Because
current event readers reject unknown required reason values, a consumer that
reads durable runs containing this optional reason must use the updated event
codec; the implementation plan includes the compatibility test and
documentation.

## 6. Provider rollout boundary

The hook is opt-in per adapter, not implied by
`ProviderCapabilities.prompt_cache`. `prompt_cache=True` only permits
existing cache intent/usage behavior.

Initial exact adapters should use the same pure translator called by their
stream path:

- OpenAI Chat and the OpenAI-compatible subclasses (vLLM, SGLang, DeepSeek,
  and llama.cpp): derive parts from the final `_build_payload()` result,
  including provider-specific `extra_body`/cache controls.
- Anthropic: derive parts from `_build_payload()` after native/compatible
  mode, translated system cache markers, translated tools, and cache settings
  have been resolved.
- OpenAI Responses: derive parts from `build_payload()` and include the
  `previous_response_id`/input-chain boundary when present.

Gemini and any custom provider whose SDK performs an opaque transformation may
remain unsupported (`None`) until an adapter can prove the exact final wire
surface. An unsupported adapter gets the existing model/tool advisory
behavior only; there is no approximate descriptor.

## 7. Acceptance tests

Tests are network-free and must pin the following:

1. Canonical mapping-key order produces one fingerprint; `parts` reorder
   changes it; duplicate/invalid labels, unknown keys, NaN, cycles,
   malformed digests, provider/model mismatch, and non-JSON values are
   rejected without an event.
2. A fake opt-in adapter builds one payload and returns a descriptor derived
   from that exact payload. The test computes the expected component digests
   from the captured final wire projection and asserts the core fingerprint
   matches. A second request with a hook-mutated system block or tool order
   changes only the expected component(s).
3. A `BeforeProviderCall` hook that adds unsupported cache/tool/schema fields
   is followed by capability lowering. The adapter sees the same final
   request that `stream()` receives; the hash is over the lowered wire, not
   the pre-lowering surrogate.
4. OpenAI-compatible transformed tool order/schema, Anthropic cache-control
   placement, Responses chaining, and provider cache-setting changes produce
   exact drift. A first call has no baseline; stable repeats have no event.
5. A provider with no hook or a hook returning `None` never gets a
   `wire_prefix_changed` event. Existing model/tool advisory tests remain
   unchanged.
6. Resume and exact frozen-request replay do not persist or invent a wire
   baseline. Replaying an identical frozen request is stable; a new process
   or an unsupported observation starts without a comparison.
7. A descriptor containing an API key, authorization header, prompt text,
   schema text, URL, or secret-like field is rejected or never surfaced.
   Event details, event JSON, run timeline/report, stdlib logs, and observer
   payloads contain no secret. Component digests and raw descriptor data are
   never serialized into an event or checkpoint.
8. An adapter hook that raises or returns malformed data is debug-logged and
   does not alter request dispatch, provider usage, or run success.

The lifecycle suite must also prove that an auxiliary compaction provider call
can settle its separate accounting receipt without changing the primary
Session descriptor/fingerprint. Accounting and prefix-drift observability are
disjoint: no auxiliary call emits a primary wire advisory or consumes the
primary baseline.

## 8. Exact implementation surface

The implementation plan owns the following code boundaries and no unrelated
product behavior:

- `src/linch/_prompt_cache.py`: strict descriptor validation,
  component/canonical digests, fingerprint, and redacted diff helpers.
- `src/linch/providers/base.py` and
  `docs/architecture/provider-contract.md`: optional adapter hook and
  exactness/`None` contract.
- `src/linch/loop/request.py` and `src/linch/loop/runner.py`: final
  capability lowering, hook timing, observation, and ephemeral-baseline
  updates.
- `src/linch/session.py`: private non-durable baseline fields.
- `src/linch/events.py`, `src/linch/reports.py`, and
  `docs/architecture/events.md`/`docs/usage/providers.md`: additive
  reason, codec/report behavior, and user-facing advisory semantics.
- `src/linch/providers/openai_chat.py`,
  `openai_responses.py`, and `anthropic.py`: exact hooks using their
  existing payload builders; inherited OpenAI-compatible subclasses are
  covered by adapter tests.
- `tests/test_wire_prefix_advisory.py`, the existing prompt-cache tests,
  and provider payload/conformance tests: acceptance coverage above.

The streaming attempt loops in src/linch/loop/streaming.py are included in the
boundary so the callback is ordered before every physical provider stream.

No prompt/persona or public request field is added or changed by the wire
observation. The final capability downgrade may sanitize fields a hook added
that the provider already declared unsupported; that is existing capability
policy, not a prefix decision. No provider normalized stream event, cache
control, limiter, persistence schema, or OpenTelemetry span is changed by the
observability feature.

# Migrating to Linch 3.0.0a1

`3.0.0a1` is an incremental prerelease for Release-A model-call accounting.
It keeps the Linch 2.x neutral `Agent` defaults, persona, prompts, and opt-in
feature behavior. This guide covers only the accounting slice; the later 3.0
reliability policy and other roadmap lanes are not shipped here.

## Pin the prerelease deliberately

Pin this prerelease while reviewing the new accounting fields and durable-run
semantics:

```toml
dependencies = ["linch==3.0.0a1"]
```

Keep the existing optional extras and their dependency floors. No unrelated
dependency upgrade is required for this feature.

## Accounted usage is cumulative

Every physical provider delivery that reaches normalized `message_end` with
valid `Usage` is settled once by delivery ID. The run total includes:

- primary model deliveries;
- built-in compaction deliveries; and
- calls made by a custom compaction strategy through Linch's supplied provider
  facade.

Retries and fallbacks receive new delivery IDs and count separately when they
reach final usage, even if they reuse one logical operation. A compaction
response can be rejected because it is empty, non-text, tool-using, oversized,
or otherwise unusable; valid final usage is still charged.

The existing `UsageEvent` remains compatible. Auxiliary compaction usage adds
`source="compaction"`, `call_id`, `logical_call_id`, and `model`. The public
`ModelCallEvent` / `is_model_call_event` pair exposes the typed journal envelope
for store and journal observers; ordinary primary delivery records remain
journal-only and are not extra normal stream events.

`ResultEvent.total_usage` and run reports expose the cumulative known total. If
a dispatched delivery is cancelled, fails, or ends before valid final usage,
the usage is unknown rather than zero and the terminal result sets
`usage_complete=False`. An uncapped run may finish with that lower bound.

## Budget behavior changes at the compaction boundary

Compaction is charged against the same run budget as the primary turn. Receipt
settlement happens before summary application, hooks continue, or another
provider dispatch. The loop rechecks admission after compaction, so a summary
that exhausts a token or USD cap prevents the main turn from starting.

For a capped run, unknown usage is a strict fence: Linch stops before another
model call rather than claiming that the configured cap is enforced. A
pre-dispatch cancellation or admission denial is not billed; a dispatched call
without valid final usage is not silently treated as free.

The same live `RunBudget` object shares receipt ownership and charged delivery
IDs across the in-process agent/subagent tree. A resume may adopt a checkpoint
with a fresh zero-spent budget; a fresh nonzero or owner-mismatched capped
budget fails closed rather than replaying charges. These are in-process
guarantees only: distributed-tree recovery, exactly-once external-provider
billing, and reservations against concurrent admissions are not promised.

## Durable compaction replay

`DefaultCompaction` and `DetailedCompaction` retain their existing strategy
semantics and prompts. In a durable run, an accepted summary carries a bounded
receipt-backed result and an ordered hash-only source recipe. Required invoked
skill bodies are flushed through the existing session store before the recipe
is written. On resume, a matching source, strategy configuration, run
contract, and canonical skill-listing wrapper allow Linch to apply the stored
summary without another provider call or duplicate charge.

Linch fails closed when the source order, durable history boundary, receipt,
skill body, listing wrapper, or contract does not match. A provider-view cache
is verification material, not source authority. Old in-flight accounting state
also fails closed, even when legacy resume is explicitly enabled; do not ask
Linch to guess whether an interrupted delivery was billed.

## Custom and standalone compaction

The public `CompactionStrategy` protocol is unchanged. A custom strategy that
uses the provider facade receives accounting, run cancellation, and one
provider-slot/limiter acquisition per physical stream. Opaque network calls
outside that facade are not observable by the SDK. Arbitrary multi-call or
transform behavior has no automatic deterministic paid-result replay guarantee;
provide an explicit deterministic result/commit seam if the host needs one.

The standalone `summarize_with_provider(...)` helper remains available with
its existing direct-provider behavior. Calling it outside the normal run does
not create a durable run receipt or add usage to a `RunBudget` unless the host
integrates that accounting itself.

## Pricing and compatibility limits

Unknown or unpriced models never receive a synthetic USD price. Their reported
tokens remain in cumulative usage, while cost fields and known-cost aggregates
are partial (`None` where no price is known). A USD cap cannot enforce an
unknown price; use a custom pricing table when the host knows the model's
rates.

This prerelease adds no reliability correction policy, bounded feedback,
default-on reliability controls, distributed accounting, exact external
billing, reservation protocol, or other later 3.0 feature lane. Existing
neutral prompts/persona/default compaction behavior stays unchanged.

See [Events](usage/events.md), [Agent & session](usage/agent.md), and
[Compaction architecture](architecture/compaction.md) for the operational
details.

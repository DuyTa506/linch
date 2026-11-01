# Token-budget closed-chunk compaction

**Status:** implementation contract for the 2026-10-30 integration work.  This is
an opt-in strategy; it does not change the existing default compaction prompts,
persona, or `CompactionLadder` behaviour.

## Goal and non-goals

When a provider-view history is over a caller-selected input-token target, replace
only a safely closed, old range with a bounded summary.  The durable session log
(`full_history`) remains append-only and byte-for-byte available; only a new
provider-view projection is recorded.  The design is Python-native and must not
copy GPL code or prompts.

It is not a general history rewriter, a tool-output recovery implementation, or a
change to default agent behaviour.  If a safe reduction cannot be made, it must
leave the projection unchanged.

## Public surface and compatibility

```python
TokenBudgetCompaction(
    target_input_tokens: int,
    *,
    keep_recent_chunks: int = 8,
    max_summary_output_tokens: int = 2_048,
)
```

All three values are positive.  The strategy is selected explicitly through the
existing `Agent(..., compaction=...)` surface.  It keeps the existing
`CompactionStrategy.compact(context, provider)` protocol, so custom and standalone
strategies continue to work.  `CompactionContext` gains only defaulted,
backward-compatible optional facilities:

* `estimate_tokens(messages) -> int`, supplied by the runtime from its normal
  estimator (with the existing deterministic JSON/4 fallback);
* `auxiliary_caller`, the common accounted model-call gateway when running inside
  an Agent; and
* runtime budget/window metadata sufficient to cap the final provider input.

The chunk parser, estimator adapter, selector, renderer, and projection recipe
are private helpers.  No new public chunk model is required.

## Closed-chunk safety grammar

A chunk is a whole-message range.  No `Message` or content block is split.

1. An ordinary message without `ToolUseBlock` is one complete chunk.
2. An assistant message containing one or more tool uses begins one exchange.  It
   closes only after every declared tool-use id has exactly one matching
   `ToolResultBlock`; results may span multiple following user messages and may
   arrive in any order (parallel calls).
3. A duplicate, unknown, missing, or orphan result; a second assistant turn before
   closure; or an unsupported/ambiguous layout makes the affected region protected.
   Protected and open regions are retained verbatim, never guessed at or partly
   summarized.
4. Activation/control messages (including skill/system-reminder injections and
   their explicit provider metadata marker) are durable anchors.  They partition
   eligible history and remain verbatim in the provider projection.

The last `keep_recent_chunks` complete chunks, the current prompt, all control
anchors, and all malformed/open regions are retained.  To preserve chronology with
a single model call, one invocation summarizes at most one contiguous old run of
eligible closed chunks; anchors partition runs.  Repeated normal compaction may
handle another run later.  This deliberately prefers fidelity over an unsafe
attempt to reach a target in one pass.

## Budget, rendering, and acceptance

The effective final-input cap is the minimum of `target_input_tokens` and the
runtime's provider-window allowance after reserved completion tokens and a small
deterministic safety margin.  Selection uses the supplied estimator and chooses
only whole oldest chunks.  It reserves `max_summary_output_tokens` before
dispatching the summary request.  If protected anchors alone exceed the cap, or
there is no eligible run, it returns the original projection with a diagnostic
rather than dropping facts.

The selected range is rendered deterministically as compact canonical JSON inside
a textual closed-context envelope.  It contains role order, tool names/inputs,
tool-result status, and recovery references, but not response-chain metadata.
Binary/image/thinking/unknown content is protected in v1 rather than lossy
serialized.  The summary request asks for a compact chronological replacement;
the resulting replacement is accepted only when it is valid, non-blank, and the
estimated resulting projection strictly shrinks and fits the cap.  Otherwise no
projection is recorded.

## Recovery, accounting, cancellation, and replay

Full raw tool output remains in `full_history`.  A recovery-aware projection may
already replace output with a typed `ResultArtifactRef` and
`ToolResultProvenance`; the compactor must retain those references in both its
source manifest and replacement.  It consumes only the private helpers in
`src/linch/tools/projection.py`:
`extract_recovery_refs(messages) -> list[ResultArtifactRef]` and
`render_recovery_manifest(refs) -> Message`.  It never accesses raw artifact
bytes or defines artifact storage policy.

The scheduler's no-recompression guard applies only to an immediately emitted
recovery result while that result is being projected/offloaded.  An older recovery
page in a safely closed historical range may be summarized or elided normally;
its reference remains in the manifest and in `full_history`.  There is no blanket
exemption for recovery history.

Within an Agent, every summary call goes through the new common accounted
auxiliary caller: one admission/budget decision, one provider limiter slot, one
receipt, the originating `AbortContext`, and no nested provider slot.  Budget
rejection, circuit-breaker rejection, or cancellation records no new projection;
after provider dispatch, the common caller owns normal accepted/rejected/unknown
usage accounting.  Direct standalone use without that facility may retain the
legacy provider fallback, explicitly outside runtime accounting.

The recorded projection recipe includes strategy/version/config, selected source
fingerprint, target fingerprint, recovery-manifest fingerprint, and the accounted
receipt/projection id.  Resume/fork reuses an already accepted result only when
the source fingerprint exactly matches; a mismatch fails closed and never silently
issues a paid replay call.  Snapshot persistence and receipt ordering follow the
accounted-model-call contract.

## Required deterministic evidence

Tests must cover parallel and multi-message tool results; orphan/duplicate/missing
results; control anchors; recent-tail retention; exact estimator boundaries;
over-budget/no-safe-range no-op; blank/oversize/non-shrinking summaries; immutable
full history and recoverable refs; cancellation and one gateway/limiter admission;
and exact-match replay versus source mismatch.  Existing default-compaction prompt
snapshot tests must remain unchanged.

## Fixed integration boundaries

1. Recovery provenance is owned by `src/linch/tools/projection.py`; token
   compaction consumes its typed refs and the two helpers above, with no raw
   artifact access.
2. The common caller is `src/linch/model_calls.py`, supplied as
   `CompactionContext.auxiliary_caller`.  Its paid-projection schema is a hard
   dependency on the pre-landed accounting Task 4; its final concrete field names
   are supplied at implementation dispatch, not invented by this strategy.
3. `TokenBudgetCompaction` has an approved deliberate public export.  The root
   ledger selects the prerelease/version label only at final preparation; no worker
   publishes a package.

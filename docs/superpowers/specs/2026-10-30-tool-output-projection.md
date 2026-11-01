# Linch 3.0 recoverable tool-output projection

Status: implementation authority for the projection slice that follows accounted
model calls and tool discovery. This is a Python-native design; it introduces
no OpenHuman code, GPL code, service dependency, prompt change, or model wire
format change.

## Outcome and release boundary

Linch currently sends a full ToolResultBlock to provider history. Its optional
filesystem offload replaces only that provider-facing text with a path preview,
while ToolCallEndEvent retains the original ToolResult. That is useful for
large text, but it does not provide a typed, scoped recovery reference,
immutable provenance, or a safe paging boundary.

This slice adds an **opt-in, per-tool** projection policy. A configured tool
stores its final original output in a scoped artifact store, puts a bounded
deterministic projection plus recovery footer in full history/provider view,
and exposes a normal permission-checked read_tool_result tool for bounded
pages. Tools with no policy retain their current behavior, including existing
result_offload behavior.

The first release includes deterministic preview and V2 JSON-focus projection.
It intentionally excludes model-generated summaries, general output
compression services, token-budget/chunk compaction, cross-session artifact
sharing, automatic retention cleanup, and changes to prompts/persona. A future
model summary must be a separately specified auxiliary ModelCallKind routed
through the accounted caller; it is not silently added here.

This feature does not block durable activation. Activation may rely on the
existing immutable full_history plus common control-field copy/projection
invariants. Conversely, this feature must preserve any future inline control
fields by copying a ToolResultBlock rather than rebuilding it from content.

## Approaches considered

1. Extend OffloadConfig and use read_file. Rejected: an exposed virtual
   filesystem path has no result-specific scope check, no opaque typed ref, and
   no durable provenance contract. It also conflates pre-existing default
   offload behavior with a new opt-in policy.
2. Store every original in Session full_history. Rejected: it defeats context
   reduction, persists large payloads in the transcript, and has no bounded
   paging surface.
3. Use a small scoped ToolResultArtifactStore and a dedicated recovery tool.
   Selected: it makes authorization explicit, keeps full history immutable and
   compact, and works with an in-memory test store or a private durable native
   FileBackend adapter without exposing its paths to the model.

## Goals and non-goals

Goals:

- Per-tool opt-in preview/truncation and explicit V2 focus.
- Retain the original before publishing a ref; support bounded pages and
  immutable provenance.
- Keep the typed ref through session codec, ToolCallEndEvent recovery,
  checkpoint recovery, reload, and provider-view compaction copies.
- Run the recovery reader through ordinary scheduler validation, permission,
  hooks, cancellation, telemetry, and tenant/session authorization.
- Preserve no-policy behavior and existing ToolResult/ToolOutput V2 contracts.

Non-goals:

- No model summarization, hidden model charge, budget reservation, or new
  prompt instruction.
- No change to full-history append-only semantics. The projected block is the
  immutable historical result; the original lives in the artifact store.
- No change to DefaultCompaction, DetailedCompaction, CompactionLadder, or a
  new token/chunk compactor. Future compaction may use the provenance field but
  is a separate feature.
- No tenant chosen from tool arguments, model output, artifact ref text, or a
  memory-cache hit. No cross-session recovery in this first release.

## Public contracts

Add the following deliberate exports through linch.tools and lazy linch
top-level exports. They are frozen/slotted data contracts except the protocol.

~~~python
ProjectionMode = Literal["preview", "focus"]

@dataclass(frozen=True, slots=True)
class ToolOutputProjectionPolicy:
    mode: ProjectionMode
    max_inline_chars: int = 4096
    focus_pointers: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class ResultArtifactScope:
    scope_key: str       # trusted opaque host scope, never model input
    session_id: str      # runtime-filled from ToolContext

@dataclass(frozen=True, slots=True)
class ResultArtifactRef:
    artifact_id: str     # store-generated opaque random capability locator
    sha256: str
    byte_length: int
    media_type: Literal["text/plain", "application/json"]

@dataclass(frozen=True, slots=True)
class ToolResultProvenance:
    schema_version: int
    mode: Literal["preview", "focus", "recovery"]
    artifact: ResultArtifactRef | None
    source_sha256: str | None
    source_bytes: int | None
    policy_fingerprint: str | None
    recovered_from: ResultArtifactRef | None = None

@dataclass(frozen=True, slots=True)
class ResultArtifactPage:
    ref: ResultArtifactRef
    text: str
    next_cursor: str | None
    complete: bool

class ToolResultArtifactStore(Protocol):
    durable: bool
    async def put(
        self, *, scope: ResultArtifactScope, artifact: ToolResultArtifact,
        signal: AbortContext | None = None,
    ) -> ResultArtifactRef: ...
    async def read_page(
        self, *, scope: ResultArtifactScope, ref: ResultArtifactRef,
        cursor: str | None, max_chars: int,
        signal: AbortContext | None = None,
    ) -> ResultArtifactPage: ...

ResultScopeResolver = Callable[[ToolContext], str]

@dataclass(frozen=True, slots=True)
class ToolOutputProjectionConfig:
    store: ToolResultArtifactStore
    scope_resolver: ResultScopeResolver
    max_artifact_bytes: int = 4 * 1024 * 1024
    max_page_chars: int = 16 * 1024
    require_durable_store: bool = True
    recovery_tool_name: str = "read_tool_result"
~~~

ToolResultArtifact is an internal normalized record. It contains only the
final rendered text, optional canonical V2 JSON value, source tool name,
tool-use ID, run ID, and session ID. It never contains a ToolContext, raw
request, credentials, scope key, provider metadata, or arbitrary legacy
ToolResult metadata. The store generates artifact_id; a caller cannot select
it. put is idempotent for the trusted tuple (scope, run_id, tool_use_id) only
when the immutable source digest matches; a conflict fails closed.

InMemoryToolResultArtifactStore is supplied for direct/unit use and advertises
durable=False. FileBackendToolResultArtifactStore is supplied for hosts that
give it a **private**, non-session-exposed FileBackend; it is configured with
its durable capability. The adapter namespaces records with a hash of scope
and session and never surfaces backend paths. Agent setup rejects using its
artifact backend as the Session filesystem/execution filesystem, because
read_file or ls would otherwise bypass result-store authorization. A config
requiring durability rejects a non-durable store at setup, rather than claiming
restart recovery it cannot provide.

ToolOutputProjectionPolicy is an optional duck-typed output_projection field
on a tool. FunctionTool and @tool accept output_projection=, while handwritten
tools may declare it directly. None is the default. Registry validation rules:

- preview has no focus_pointers; max_inline_chars is in 1..65,536.
- focus requires a V2 output_schema, 1..16 distinct RFC 6901 JSON pointers,
  no empty/root pointer, and the same max-inline bound.
- ToolOutputError and ordinary legacy error ToolResult values remain inline;
  focus is never applied to an error.
- The recovery reader is projection_passthrough and cannot itself configure an
  output_projection.

The agent gains tool_output_projection: ToolOutputProjectionConfig | None.
At construction it rejects a registered policy without this config, a recovery
tool name collision, an invalid resolver/config bound, or an unsafe artifact
backend identity. The config registers exactly one private ReadToolResult
instance under recovery_tool_name. It is an ordinary read-scoped tool and is
not installed on the default/no-config path.

## Projection and recovery semantics

Projection occurs after execution has settled and after PostToolUse mutations,
so hooks inspect and can replace the original final result. It runs once for
all success paths, including a hook-supplied/precomputed result. Existing
automatic filesystem offload is skipped for a call with output_projection;
unconfigured calls retain the existing offload path unchanged.

For a configured successful call:

1. Normalize the final source text and, for V2, its canonical JSON output.
   Enforce max_artifact_bytes before storage.
2. Resolve ResultArtifactScope by calling the host resolver with ToolContext,
   validating a nonempty opaque scope key, and filling session_id from the
   runtime context. No tool/model input participates.
3. Store the original atomically/idempotently. A ref exists only after put
   returns; its digest covers the retained rendered text.
4. Build the provider/history block by dataclasses.replace on the existing
   ToolResultBlock, changing only content and provenance. This preserves future
   activation/control fields.
5. Include a deterministic bounded footer after the body:

   [Original output retained: ref=<opaque>, sha256=<prefix>, bytes=<N>.
   Use read_tool_result(ref="<opaque>", cursor=null) for the next page.]

preview uses the first max_inline_chars of final text. focus deterministically
selects the declared JSON pointer values from the V2 canonical value, renders
the pointer/value map as canonical JSON, then applies the same bound. A missing
focus pointer produces a ref-only footer, not a fallback that silently reveals
unfocused raw text. No LLM is used for either mode.

ReadToolResult validates ref/cursor/max_chars, goes through normal scheduler
permission and hooks, resolves the same trusted session+scope, and asks the
store for no more than config.max_page_chars. Missing, wrong-scope, expired,
or corrupt refs all return the same bounded “Result unavailable.” error to
avoid an existence oracle. A successful page has provenance mode=recovery,
includes its next-cursor footer, and is projection_passthrough: it is never
reprojected, reoffloaded, focused, or sent to an auxiliary model. This is the
meaning of “no recompression of recovery output” in this release. Existing
provider-view compaction is untouched; it may later summarize or elide an old
recovery page. This feature does **not** promise an eternal provider-visible
breadcrumb. Immutable full_history retains the typed ref/provenance, and
micro-compaction copies retain it, so a later authorized recovery call can
still retrieve the original.

## Durability, failure, and isolation

ToolResultBlock gains optional provenance: ToolResultProvenance | None. Its
codec is strict/versioned and omits the field for legacy blocks. Provider
adapters keep consuming only content/is_error, so provenance never reaches a
provider wire. Full history contains the bounded block/ref and is never
mutated after append; raw output is never reconstructed from a compacted
summary or event text.

ToolCallEndEvent gains optional result_block: ToolResultBlock | None. Scheduler
sets it to the final projected block; event codecs serialize it through the
same block codec. _tool_result_block_from_end prefers result_block and falls
back to its legacy string reconstruction. This is required so a crash after
tool execution but before result-message append retains the typed ref through
the event journal and RunCheckpoint.completed_tool_results.

If a non-inline policy cannot persist its artifact, or cancellation occurs
during projection, it must never publish a dangling ref or the full raw value.
The scheduler emits a safe paired error result whose fixed text states that the
tool already executed, its output could not be retained safely, and it must
not be automatically rerun. It contains no raw output, scope, ref, backend,
or storage-error detail. It marks a private ToolProjectionPersistenceError
terminal condition, and the runner stops before another provider request.
For a parallel batch, it first preserves/persists end blocks for every sibling
that has already settled, supplies safe terminal/aborted pairing blocks for
any unfinished calls, and appends one valid paired tool-result message before
the terminal result. The underlying tool may already have caused a side effect;
existing ToolContext.idempotency_key remains the integration's retry
deduplication mechanism. A successfully stored artifact followed by a crash
before the end event can leave an unreachable orphan, which is safe and is not
an authorization grant.

The artifact store enforces both scope_key and session_id; an opaque ref alone
is not authorization. Ref text, footer, and event provenance omit scope_key
and backend paths. A fork is a different session in v1, so its copied ref is
intentionally unavailable unless a later explicit lineage-sharing design is
added. Same-session durable reload/resume works when the same durable store and
scope resolver are configured. No artifact-store result is cached in memory or
used as authority for discovery, activation, or memory scope.

The run contract fingerprints the effective per-tool policy fingerprints,
projection codec version, recovery tool name, page/artifact bounds, and durable
store requirement. A mismatch on a frozen/resumed run fails closed. The feature
uses no RunCheckpoint extension namespace and never overwrites
linch.model_calls.v1, linch.reliability, or future tool-surface state.

## Accounting and compaction seams

This slice is deterministic local transformation and storage. It creates no
ModelCallEvent, UsageEvent, token/cost charge, budget admission, or provider
slot acquisition. It propagates the current tool AbortContext into store calls
and honors cancellation. A later summarizing mode must be separately designed
to use the accounted auxiliary caller with fresh logical/delivery IDs and to
recheck budget before the next primary dispatch.

Current micro compaction must use dataclasses.replace or equivalent copying so
provenance survives content elision. Existing full/provider-view compaction is
not changed or made a prerequisite. Token-budgeted closed-chunk compaction is a
separate future release that must preserve ToolUse/ToolResult pairing and these
refs; this specification neither implements nor assumes it.

## Acceptance matrix

1. A policy-free legacy tool produces its exact prior block/offload behavior.
2. Preview stores original, writes bounded body/footer/provenance, and preserves
   raw only in the authorized artifact store.
3. V2 focus accepts only declared pointers, never exposes an undeclared field,
   and falls back to ref-only when a declared path is absent.
4. Same-session authorized paging is bounded and deterministic; wrong tenant,
   wrong session, forged ref/cursor, missing record, and corrupt digest all
   produce the same unavailable error.
5. Recovery pages bypass projection/offload and retain provenance; they do not
   trigger a model call or alter accounting/budget totals.
6. Projection artifact write/cancellation failure yields no ref/raw history,
   uses the fixed “already executed / do not automatically rerun” safe error,
   preserves all settled parallel sibling results plus valid pairing blocks for
   unfinished calls, and makes no subsequent provider request.
7. Event codec, checkpoint recovery, session-store reopen, and a copied
   provider-view/micro-compaction block preserve the ref; same-session resume
   reads the retained original without reexecuting the source tool.
8. Normal permission denial of read_tool_result happens before store access;
   granted permission still cannot cross the store’s scope/session fence.
9. Config/registry/run-contract validation rejects an unconfigured policy,
   recovery-name collision, invalid focus policy, unsafe public filesystem
   backend, or resumed policy mismatch.

## Implementation order

1. Contracts/codecs and registry validation.
2. Scoped artifact stores and recovery tool unit behavior.
3. Agent setup/configuration and run-contract fingerprints.
4. Scheduler/event/checkpoint/runner integration and crash tests.
5. Documentation, full feature review, and a 3.0 prerelease preparation slice.

Every step is independently reviewed. Activation and token compaction may land
before or after this work when their own invariants are satisfied; neither may
reinterpret an artifact or projection as an authority source.

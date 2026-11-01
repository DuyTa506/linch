# Tool discovery and durable activation

Status: consolidated design authority for the opt-in tool-discovery feature.
This document is a contract for implementation and tests.  It does not
authorize model-selected code, package, server, URL, command, or MCP
configuration.  It is intentionally independent of the new raw-artifact and
token-budget-compaction feature lanes.

Target: Python >=3.10, existing async Linch runtime, no new dependency.

## 1. Goal, release boundary, and non-goals

An embedding host can opt in to a single trusted `tool_search` tool that lets a
model discover safe metadata for already registered native tools, already
configured MCP tools, and loaded skills.  When the host also supplies an
activation policy, the same successful `tool_search` call may atomically grant
the policy-approved subset of its returned deferred tools.  Those schemas first
appear on the next provider request.

The feature is off by default.  A bare `Agent`, ordinary default-active tools,
the existing `Skill` loading behavior, and configured MCP connection behavior
remain unchanged when `tool_discovery=None`.

Non-goals:

- There is no `activate_tools` follow-up tool, model-supplied server install,
  dynamic import, package installation, executable loading, or arbitrary tool
  object construction.
- Search never returns an input/output schema, contract fingerprint,
  activation identity, policy reason, server endpoint/command/header/env,
  skill body, substituted skill arguments, allowed-tools overlay, or model
  override.
- Skills are catalog metadata only.  They continue to execute through the
  existing `Skill` loader and are never activation targets.
- This release has no deactivation/revocation fact.  A scope, policy, reader,
  or descriptor mismatch fails closed; it is not silently treated as a
  revocation.
- New raw-result artifacts and a new token-budget compaction system are not
  prerequisites.  Existing immutable `full_history` is authority and existing
  compaction is a copy-on-write `provider_view` projection.

The accounting feature may land first in the release sequence, but discovery
does not consume its ledger, receipt, or compaction APIs.

## 2. Deliberate public API

Create `src/linch/tool_surface.py`.  Add exactly these lazy top-level exports
to `linch.__all__` and matching `TYPE_CHECKING` imports:

```python
ToolDiscoveryConfig
ToolCatalogScope
ToolCatalogRequest
ToolCatalogPolicy
ToolActivationCandidate
ToolSearchActivationRequest
ToolSearchActivationDecision
ToolSearchActivationPolicy
ToolActivation
```

`AgentOptions` and `Agent.__init__` gain only:

```python
tool_discovery: ToolDiscoveryConfig | None = None
```

It is not a `FeatureFlags` member.  `None` means disabled and preserves the
current request, registry, session, and public behavior byte-for-byte.

The public records/protocols are frozen slotted dataclasses or runtime-checkable
protocols with these fields and methods:

```python
@dataclass(frozen=True, slots=True)
class ToolCatalogScope:
    scope_id: str
    scope_fingerprint: str
    eligible_tool_names: tuple[str, ...]
    eligible_skill_names: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class ToolCatalogRequest:
    session_id: str
    run_id: str | None
    turn_index: int
    deps: Any

class ToolCatalogPolicy(Protocol):
    policy_id: str
    policy_version: str
    async def resolve(self, request: ToolCatalogRequest) -> ToolCatalogScope: ...

@dataclass(frozen=True, slots=True)
class ToolActivationCandidate:
    name: str
    activation_id: str
    activation_version: str | None
    tool_contract_fingerprint: str

@dataclass(frozen=True, slots=True)
class ToolSearchActivationRequest:
    session_id: str
    run_id: str
    turn_index: int
    tool_use_id: str
    query: str
    scope: ToolCatalogScope
    candidates: tuple[ToolActivationCandidate, ...]
    deps: Any

@dataclass(frozen=True, slots=True)
class ToolSearchActivationDecision:
    approved_names: tuple[str, ...] = ()
    reason: str | None = None

class ToolSearchActivationPolicy(Protocol):
    policy_id: str
    policy_version: str
    async def evaluate(
        self, request: ToolSearchActivationRequest
    ) -> ToolSearchActivationDecision: ...
```

`reason` is bounded to 256 characters, is diagnostic-only, and is neither
durable nor provider-visible.  The runtime rejects duplicate names, names not
in the exact deferred candidate tuple, active names, unregistered names,
identity-less targets, and decisions over `activation_limit`.

```python
@dataclass(frozen=True, slots=True)
class ToolDiscoveryConfig:
    catalog_policy: ToolCatalogPolicy
    activation_policy: ToolSearchActivationPolicy | None = None
    search_limit: int = 5       # inclusive range 1..10
    activation_limit: int = 8   # inclusive range 1..8
```

`ToolActivation` is a frozen JSON-safe durable record.  It is defined in
`types.py` to avoid a codec cycle, then re-exported by `tool_surface.py`:

```python
@dataclass(frozen=True, slots=True)
class ToolActivation:
    schema_version: int
    name: str
    activation_id: str
    activation_version: str | None
    tool_contract_fingerprint: str
    scope_id: str
    scope_fingerprint: str
    catalog_policy_id: str
    catalog_policy_version: str
    activation_policy_id: str
    activation_policy_version: str
    source_loader_id: str             # exactly "linch.tool_search.v1"
    source_tool_use_id: str
```

`ToolResultBlock` gains `tool_activations: tuple[ToolActivation, ...] = ()`.
The private `ToolSurfaceReaderGuard` is a `ContentBlock` with persisted type
`"tool_surface_guard"`, `required_reader_version`, and `feature_version`.  It
is deliberately not a public root export.

## 3. One trusted control tool

When discovery is configured, `Agent` pre-registers exactly one private
`tool_search` control tool.  A collision is a `ConfigError`.  It is ordinary
with respect to schema validation, the tool pipeline, permission engine,
timeouts, telemetry, pre/post tool hooks, and scheduler ordering.  Its schema
is fixed:

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string", "minLength": 1, "maxLength": 256},
    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
    "kinds": {
      "type": "array",
      "items": {"enum": ["tool", "skill"]},
      "maxItems": 2,
      "uniqueItems": true
    }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

The implementation normalizes a nonblank query, clamps a supplied limit to the
configured limit, and ranks only the current host scope deterministically:
case-folded exact name, prefix, token containment in name/tags/description,
then `(kind, name)` as a stable tie-breaker.  It returns only:

```json
{"results": [{"kind": "tool|skill", "name": "...", "description": "...", "tags": ["..."], "state": "active|requires_activation"}]}
```

Search-only mode (`activation_policy=None`) returns the same scoped metadata,
including deferred targets, but creates no facts and exposes no deferred
schemas.

With an activation policy, `tool_search` supplies that policy only the ordered
deferred candidates returned by this particular search.  The query is untrusted
data, never authority.  The policy may approve a bounded subset; the runtime
then re-resolves scope and rechecks registry generation and every approved
descriptor fingerprint.  The complete approved subset commits as facts or none
does.  The model cannot supply a target-name list separate from search results.

The private implementation produces an opaque approval token.  Only the exact
runtime-owned `tool_search` object can mint it; a custom tool, tool-result text,
metadata, provider event, checkpoint, or hook cannot manufacture one.  Post
tool hooks run first.  A final error/block/timeout/abort clears the token.  The
runner revalidates the token immediately before copying facts into the durable
result message; a changed scope/registry causes no grant.

The phrase “search then call” means two provider requests.  The provider has
already emitted every tool use in the response containing `tool_search`, so no
safe implementation can add a target schema to that same response.  The
following request sees an approved schema only after the result append commits.

## 4. Catalog inputs and activation metadata

All targets are registered before any model request.  `ToolRegistry.register`
validates optional duck-typed attributes:

```python
activation_required: bool = False
activation_id: str | None = None
activation_version: str | None = None
```

An activation-required target needs a nonempty stable `activation_id`; an
ordinary target remains active by default.  `FunctionTool` and `@tool` accept
the same keyword arguments.  A target contract fingerprint is canonical JSON
SHA-256 over name, description, input schema, scope, parallel, tags, output
contract/renderer identity, and activation metadata.  Any changed fingerprint
invalidates a durable fact.

Configured MCP remains host-controlled.  `McpStdioServerConfig` and
`McpHttpServerConfig` gain host-only `catalog_id`, `catalog_version`, and
`activation_required_tools: tuple[str, ...] = ()`, where names are raw MCP
tool names.  Connection validates every listed raw name exists, and its wrapper
receives `activation_required=True`, an id derived from
`"mcp:<catalog_id>:<raw_name>"`, and `catalog_version`.  No endpoint, command,
headers, environment, or model input enters an activation record.  `tool_search`
does not connect a server or call `add_mcp_servers`.

Skills are never targets.  When discovery is enabled, suppress the static
all-skills system listing; catalog results are the metadata discovery path.
Keep the existing `SkillTool`, substitution, and pending overlay behavior, but
inject a scope callback that rejects a requested skill outside the current
`eligible_skill_names`.  A skill’s `allowed_tools` overlay can only narrow the
already-derived offered surface.  The generic `Skill` loader schema is offered
only when it is registered, survives the context selection, and the resolved
scope has at least one eligible skill.

## 5. Immutable offered surface

`tool_surface.py` owns a private immutable `ToolSurfaceSnapshot`, stored on the
session only for the current turn and encoded under
`RunCheckpoint.extension_state["linch.tool_surface"]`:

```text
schema_version, turn_index, scope_id, scope_fingerprint,
registry_generation, ordered (name, contract_fingerprint) descriptors,
branch_activation_fingerprint, context_selection_fingerprint,
surface_fingerprint
```

The extension is a cache/checkpointed exact-turn assertion, never an authority
source.  It is merged with unrelated extension namespaces.  Full history is
always re-folded.

For a discovery-enabled request, the offered set is exactly:

```text
registered session registry
  ∩ resolved catalog scope eligible tools
  ∩ (default active OR valid branch activation fact)
  ∩ context/skill name narrowing
  ∩ provider capability filtering
  + runtime-owned tool_search
  + Skill only when its scope has eligible skills
```

`ContextBuildResult.selected_tools` becomes a name-only narrowing operation in
this mode.  It may not replace the registry or introduce a new tool object.
`current_turn_allowed_tools` keeps its legacy overlay and permission semantics;
it is not the discovery authority.  The scheduler validates the exact snapshot
before canonical validation, permission evaluation, hooks, or execution.
Activation never auto-allows a later target in `PermissionEngine`.

Scope resolution is async, so the runner resolves/folds/builds this snapshot
after context building and before synchronous request construction.  Every
retry or compaction rebuild resolves a new snapshot.  `_build_turn_request`
only consumes the already prepared snapshot.  Before dispatch, the checkpoint
contains the exact snapshot; an exact model-input snapshot naturally contains
the resulting schemas.  A same-name replacement, missing target, changed
contract, changed scope, or stale snapshot fails closed.

## 6. Durable facts, guards, and projection

Authority is a fold over `session.full_history`, never `provider_view`, a
provider snapshot, `RunCheckpoint`, a run event, or a raw/offloaded artifact.
The fold accepts a fact only when all of these hold:

1. It occurs in a successful user `ToolResultBlock`.
2. Its `tool_use_id` pairs with the immediately preceding assistant
   `ToolUseBlock` named `tool_search` and the fact has loader id
   `linch.tool_search.v1` and matching source id.
3. Field/version/uniqueness/policy/scope/descriptor validations succeed.
4. The current catalog scope and live registered descriptor still match.

Errored, unpaired, non-control, duplicate, malformed, unknown-version, or
unsupported-policy facts invalidate the protected surface and fail closed.
Fold is idempotent.  An activation result does not grant in the request that
called it.

The private scheduler token is intentionally absent from `ToolCallEndEvent`
serialization and checkpoints.  Immediately before `session.append` of the
ordinary user tool-result message, the runner consumes a live token and copies
the typed facts into its `ToolResultBlock`.  Therefore:

- a durable end event or a saved `completed_tool_results` checkpoint is never
  a grant;
- crash/end-event before result append resumes with an ordinary result and no
  grant;
- an append failure discards the token and reports persistence failure;
- crash after result append re-folds the exactly one durable grant even if a
  later checkpoint was not saved.

`SessionLog` gains one common provider-projection helper.  It preserves every
control field in its owned full-history message graph, removes
`ToolSurfaceReaderGuard` blocks and removes `tool_activations` from projected
`ToolResultBlock`s, and drops a message that becomes empty.  It is used for
append, seed/reload, and optional provider snapshots.  Thus existing
Default/Detailed compaction and micro compaction may summarize/elide the
provider projection without affecting authority.  Any transform that retains a
runtime control field must use this helper/strict codec rather than construct a
bare replacement block.

Before `tool_search` can be offered in a session, append one guard-only
historical message.  It is not provider-visible.  Include a guard in every
activation result as well.  Old readers do not recognize its content type and
therefore fail while loading a protected session instead of silently ignoring
the otherwise additive `tool_activations` field.  New readers accept legacy
sessions with no guard/facts, but reject an unsupported guard/fact version or a
fact lacking the required guard.

`ToolResultBlock` and guard codecs are strict.  Built-in InMemory, SQLite, and
Postgres stores explicitly advertise private
`runtime_control_block_codec_version >= 1`; configuring discovery against an
unadvertised custom store is a `ConfigError`.  This is a deliberate fail-closed
boundary, not a promise that arbitrary duck-typed stores preserve unknown
fields.

## 7. Run contract, resume, fork, and current compaction proof

Increase `RUN_CONTRACT_SCHEMA_VERSION` and add `policies.tool_surface` with the
feature version, fixed control-loader id, catalog policy id/version, optional
activation policy id/version, and control codec version.  Dynamic scope data is
not a run contract; it is a live authority check and fact/snapshot field.

Resume order is mandatory:

1. Load run and compare run contract.
2. Run a read-only tool-surface validator over full history, pairing, guard,
   fact versions, checkpoint snapshot and live scope/registry.
3. Only after success prepare the provider, restore normal run state, call
   hooks/permissions, or execute a tool.

`resume_loop` must move provider preparation below these checks.  A protected
run with a missing descriptor, same-name replacement, changed output contract,
scope/policy mismatch, unsupported reader/fact, or stale in-flight snapshot
fails with a redacted `ConfigError` and no provider/permission/hook/tool side
effect.

`fork_session` copies encoded durable messages only.  It copies neither an
approval token, policy object, registry object, provider snapshot, nor exact
turn snapshot.  It validates full-history pairings and facts before completing
the fork.  A prefix before the activation result has no grant; a prefix after it
inherits only if the child resolves the same valid scope and descriptor.

No future compaction feature is a gate.  The bounded proof required here covers
the current `DefaultCompaction`, `DetailedCompaction`, micro compaction,
provider-snapshot reload, and fork behavior: the provider projection contains
no grant, while re-folding full history gives the same activation fingerprint.
Future output/artifact/token-compaction work must preserve this invariant and
add its own cross-tests.

## 8. Bounded prerequisite gates and release slices

Plans 11/12 remain design gates, not blanket implementation authorization.
Only these bounded prerequisites are required:

| Gate | Required proof before durable activation ships |
|---|---|
| Plan 03 store/codec | Strict guard/fact/message/snapshot codec round trips; InMemory/SQLite/Postgres reopen and copy isolation; custom-store capability rejection. |
| Plan 04 checkpoint/run-store | Detached exact-turn snapshot and extension namespace isolation; monotonic event cursor; append failure cannot become a fact. |
| Plan 05 assembled replay | One offline trace across tool search, end event, pre/post append crash, reload, and provider projection proving full history is the only authority. |
| Plan 06 resume | Read-only pairing/snapshot validator runs before provider prepare and before hook/permission/tool side effects. |
| Current compaction seam | Default/Detailed/micro/reload/fork tests prove full-history facts survive and provider-view facts never grant. |

Release order is intentionally finite:

1. **Foundation:** strict control-field codecs/projection/store conformance.
2. **Search-only:** public host catalog API and metadata-only `tool_search`.
3. **Surface:** immutable offered snapshot shared by request and scheduler.
4. **Activation:** policy-approved atomic durable facts and append boundary.
5. **Recovery:** resume/fork/current-compaction validation and cross-tests.
6. **Documentation/release prep:** docs, migration notes, prerelease metadata,
   full review and verification; workers never publish.

Each slice must compile and pass its focused tests before a fresh independent
review.  Root owns integration, release, and any conflict resolution with the
accounting/reliability work.

## 9. Required behavioral evidence

The feature is complete only when fake-provider tests prove all of the
following:

1. Unified scope returns native, configured MCP, and skill metadata only; no
   schema/skill-body/server detail leaks.
2. Deferred schemas are absent until a policy-approved `tool_search` result is
   durably appended, then appear on the next request only.
3. Search never connects/registers a model-supplied MCP server or changes the
   registry.
4. Policy-approved names are exactly a valid subset of the returned deferred
   candidates; malformed/denied/changed decisions grant none.
5. Normal permission and hook behavior still applies to both `tool_search` and
   an activated target; activation is not a permission allow.
6. Text/metadata/custom tools/events/checkpoints cannot forge a fact.
7. End-event/pre-append/post-append crashes have respectively zero/zero/one
   folded grant, independent of cache/checkpoint loss.
8. Context registry replacement cannot leak a forbidden schema; scopes,
   same-name replacements, and stale snapshots fail before side effects.
9. Default/Detailed/micro compaction, provider-view reload, and fork preserve
   the full-history result and never obtain authority from a projection.
10. Legacy unguarded sessions retain old behavior; protected sessions fail on
    an old/unsupported reader or store codec.

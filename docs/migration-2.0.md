# Migrating to Linch 2.0

Linch 2.0 makes the SDK boundary explicit: Linch supplies reusable agent
runtime mechanisms, while an application chooses the tools, prompt policy,
resource trust, and domain behavior. A coding agent is a valid application of
Linch, but the coding-agent product (and its security policy) does not become
the SDK's default personality.

## The defaults that changed

### Bare `Agent` is neutral and has no workspace authority

`Agent(model=...)` now starts with an empty `ToolRegistry`, a neutral identity
prompt, and all project-resource feature flags disabled. It does not discover
skills, subagents, MCP servers, or virtual filesystem resources merely because
the process happens to run in a checkout. This is a deliberate trust boundary
for SDK embedders and services.

Opt into a software workspace explicitly:

```python
from linch import Agent, workspace_tools

agent = Agent(model="gpt-5", tools=workspace_tools())
```

For a durable planning/subagent preset, use the opt-in deep-agent factory:

```python
from linch import create_deep_agent

agent = create_deep_agent(model="gpt-5")
```

`default_tools()` and `defaultTools` remain compatibility names for the
workspace preset, but they are not the defaults of `Agent`. New integrations
should prefer `workspace_tools()` when the intent is clear at the call site.
`FeatureFlags(skills=True, subagents=True, filesystem=True, mcp=True)` can be
provided explicitly when those project resources are trusted. MCP still needs
configured servers; enabling the flag alone does not invent a server.

### System prompts are domain-neutral

The built-in prompt now describes a configured Linch runtime and only mentions
capabilities actually present in the registry. Coding doctrine belongs in an
application-owned prompt or an explicit preset. Use
`SystemPromptConfig(replace_defaults=True, append=...)` to replace the neutral
identity, or `system_prompt=` to append application instructions.

## Tool permissions and hooks

Tool calls now follow one security order:

1. parse and validate the provider call;
2. run `PreToolUse` transformation/hooks;
3. validate the transformed input again;
4. evaluate rules and `can_use_tool` on that final canonical input;
5. persist the decision and execute.

The approval callback's Linch 1 `updatedInput` response is rejected. Mutate in
`PreToolUse`, where the resulting input is validated and permission-checked;
the callback may only return `{"behavior": "allow"}` or
`{"behavior": "deny", "message": "..."}`. This prevents a callback from
approving one path/command and executing another.

## Provider streams are strict at the boundary

Providers must yield Linch's normalized stream vocabulary and required fields;
the loop does not accept raw vendor objects or silently reinterpret malformed
events. Keep provider-specific conversion inside the provider adapter. The
portable contract is documented in
[architecture/provider-contract.md](architecture/provider-contract.md).

## Progress is observational

Tools can call `ctx.report_progress(message, data=None)` while executing. The
runtime emits `ToolProgressEvent` with `type == "tool_progress"`; progress is
not provider history or the durable run event log, is best-effort, and does not
change the final `ToolResult`. Consumers that do not need it can ignore the
event.

## Durable runs and legacy records

`RunCheckpoint` still records where execution stopped. Linch 2.0 automatically
persists a `RunContract` alongside a durable run. The contract records the
resolved model/fallback chain, system blocks, normalized provider tool schemas
(including tool scope/parallel metadata), prompt/images, output and run
options, budget limits, and the resolved permission mode/root/rules. Policy
extensions participate only when they expose a stable identity; a callback or
hook closure cannot be proven equivalent merely from its Python object. Custom
approval callbacks and policy hooks therefore need `resume_policy_id` and may
add `resume_policy_version` / `resume_policy_config`. Custom Bash backends also
require a non-`None`, JSON-safe `resume_policy_config`. Linch compares the
resulting contract before resuming. The public helpers remain available for
custom stores and migration tooling:

This also applies to policy-bearing built-in adapters such as
`ToolMiddlewareHook`, `ContextInjectionHook`, `FinalAnswerVerifierHook`, and
`StopPredicateHook` when used on a durable run: assign a stable host identity
to the adapter instance. A checkpointable hook's `checkpoint_key` only names
its state slot and is not a substitute for policy identity. Runtime package
version participates in the contract, so upgrade the runtime only after
finishing active durable runs; otherwise start a new run.

```python
from linch import RunOptions

# Normal resumes compare the persisted contract automatically.
async for event in session.resume(run_id):
    ...

# A pre-2.0 row has no contract. Opt into that unsafe migration explicitly.
async for event in session.resume(
    run_id,
    RunOptions(allow_legacy_resume=True),
):
    ...
```

An old run without a contract is called a legacy run. It is not silently
blessed as compatible: hosts must explicitly choose
`RunOptions(allow_legacy_resume=True)` (and accept that the original inputs
cannot be proven), or start a new run. A stored contract with a mismatched or
corrupt fingerprint fails closed with `RunContractMismatchError`.
The low-level compatibility helper raises that typed error; `Session.resume()`
surfaces it as a `ConfigError` with the differing contract paths.

Checkpoint and event wire compatibility remains independently versioned by
`RUN_SCHEMA_VERSION`. The contract fingerprint is not a replacement for the
event log or tool idempotency key; tool execution after a crash remains
at-least-once, so integrations must reconcile external side effects with
`ToolContext.idempotency_key`.

Detached background workers emit origin-attributed audit events when their
state changes. The detached task and its completion notification are not
reconstructed after a process restart; hosts that need durable result delivery
must persist/reconcile that work in an application-owned queue or workflow.

## Session forks

Session forking is a portable history operation: it copies a validated
conversation prefix into a new session store record, preserves relevant
metadata/skill state, and rejects a boundary inside an unanswered assistant
tool exchange. It does not copy live tasks, provider connections, or arbitrary
application state. `before_seq` is exclusive and, when supplied, must identify
an existing stored message row; custom destination `id=` requires the store's
optional atomic create-if-absent capability.

```python
child = await agent.fork_session(source_session, before_seq=42)
```

Use the public `Agent.fork_session(...)` method; do not depend on
`linch.sessions.fork` internals.

## Compaction and storage fixes

2.0 keeps provider-facing message order and tool-result pairing while making
compaction safer: provider-agnostic summaries do not assume OpenAI-specific
wire details, snapshot/restore boundaries are explicit, and malformed or
missing durable snapshots fall back to the event log. Session/run storage
allocation uses transaction-safe IDs for concurrent creation in SQLite and
Postgres. Existing stores are
read with compatibility defaults; no checkpoint migration is needed solely for
the new optional contract/progress fields.

## A practical upgrade checklist

1. Pin `linch>=2.0,<3.0` only after reviewing this page and the changelog.
2. Pass `tools=workspace_tools()` (or your own registry) anywhere a coding or
   shell-capable agent is intended.
3. Set `FeatureFlags` explicitly for trusted project resources.
4. Move permission input rewriting to `PreToolUse` and remove `updatedInput`
   from approval responses.
5. Make provider adapters emit the normalized stream contract strictly.
6. Let Linch persist and compare the run contract before resuming durable runs;
   use `RunOptions(allow_legacy_resume=True)` only for an explicit migration.
7. Treat detached background completion as an in-process convenience unless
   your application owns a durable delivery path.

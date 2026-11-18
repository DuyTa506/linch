# Kernel

> Part of the [linch-rust architecture guide](./README.md).

The Rust kernel is small. An `Agent` is a value that owns everything its runs
read: the spec, the provider, the tool registry, the permission engine, the
hook chains and the stores. Every registration returns a `Registration` that
undoes exactly that registration. Tool calls pass through a fixed
plan-then-execute path with three hook points in it. Nothing is global. Use
this page when you add a registry or an extension point, add a hook point, or
check that a change keeps agents independent.

Python Linch builds the same guarantees on a scoped IoC container
([kernel](../../../docs/architecture/kernel.md)). What differs in Rust:

- There is no `Context`, no service lookup by name and no event bus. An agent
  is bound with values passed to `AgentBuilder`, and `build` refuses a missing
  one with `BuildError::Missing` (`crates/linch-core/src/agent.rs:1-8`,
  `crates/linch-core/src/agent/builder.rs:222-231`).
- `Disposable` is `Registration`. Its `dispose` is synchronous and runs its
  undo once; a failed undo is not retried (`crates/linch-core/src/registration.rs:38-48`).
- Python's `emit`, `serial` and `waterfall` dispatchers have no Rust
  counterpart. The extension points are nineteen typed hook chains, one per
  `HookPoint`, each with its own rule for how answers combine
  ([hook chains](#hook-chains)). Twelve of them only observe: added for
  parity with Python's lifecycle hooks, they answer `()`/`{}` and cannot fail
  closed (`hooks/observe.rs`).
- There is no `ToolPipeline` with `tools/pre-execute`, `tools/execute` and
  `tools/post-execute` seams. A batch runs through `plan` and `execute`
  (`crates/linch-core/src/scheduler.rs:1-22`); the `pre_tool_use` and
  `post_tool_use` chains are the places to add behavior, and `execute` has no
  waterfall to wrap ([tool call seams](#tool-call-seams)).
- There are no plugins and no domain profiles in `linch-core`. The extension
  points are traits (`Provider`, `Tool`, `RunStore`, `SessionStore`,
  `TaskStore`, `FileBackend`, `Hook`) given to `AgentBuilder`.

## The agent is the scope

`Agent` is an `Arc<AgentInner>`. It is cheap to clone, and clones share every
binding (`agent.rs:101-105`). `AgentInner` holds what a run reads from it
(`agent.rs:108-162`):

| Field | What it is |
|---|---|
| `spec` | The serde `AgentSpec`: model, prompts, limits, compaction, budget, provider retry and fallback models (`agent/spec.rs:30-78`) |
| `provider`, `permissions`, `runs`, `sessions` | The bindings the builder requires |
| `tools` | The `ToolRegistry` of this agent |
| `hooks` | The `Hooks` chains of this agent |
| `namespace` | The key space of this agent's runs and sessions |
| `contract` | The digest of what a run needs to resume (below) |
| `families`, `worker_registry` | The subagent child runs and workers of this agent's runs and sessions |
| `registrations`, `reloads` | The registrations this agent made itself, undone on drop |

`AgentBuilder::build` (`agent/builder.rs:207-323`) checks that the provider,
permission engine, both stores and a namespace are present and that the model
and namespace are not blank. It registers the built-in tools the settings ask
for (`Skill`, `send_message`, `Subagent`, `SubagentContinue`, `TaskStop`,
`tool_search`) in the agent's registry, then digests the contract. Nothing in
`build` does I/O (`builder.rs:24-26`).

### The run contract

`contract_digest` hashes the spec, the tools registered at build with their
scopes, the hook policy digest and the other features
(`builder.rs:285-288`). Every run stores the digest. `resume` and `approve`
refuse a run whose digest differs from the agent's with
`RunError::ContractMismatch` (`runner/start.rs:425-431`). The hook policy
digest covers each hook's point, `policy_id` and `policy_version`, in chain
order (`hooks.rs:84-100`), so changing a hook's behavior means bumping its
`policy_version` (`hooks/hook.rs:76-78`). That covers what a hook answers,
`retry` included. The kernel's own limit on `stop` retries (one per run
call) is a constant, not a setting, so it is not in the digest; it is not
checkpointed either, and a resume starts it afresh, as Python's
`_MAX_FINAL_ANSWER_REENTRIES` does. An agent with a `verification` spec
bounds them with its `max_retries` instead, a setting in the digest with the
rest of the spec, counted from durable `verification` events so a resume
keeps it. The spec's `redaction` and `verification` are left off the wire
when unset, so an agent without them keeps its digest
(`tests/e2e_guardrails_off.rs` pins it). The permission policy is a
feature (`permissions`) when it is not the default one
(`PermissionPolicy::is_default`: mode `default` and no layers, `auto_allow`,
path or command fields), so a run is never resumed or approved under a
policy that allows more or less than its own, as Python's contract
(`src/linch/loop/contracts.py:441`) holds it. The default policy decides
nothing by its `project_root`, so it is left out whatever the root, and the
digests of default-policy agents stay as they were
(`tests/e2e_policy_contract.rs` pins one, taken before the policy counted);
a non-default policy goes in whole, root included. `Agent::cancel` does not
check the contract. The spec's `provider_retry` and
`fallback_models` are in the digest with the rest of the spec, so a resumed
run's fallback, read back from its `model_fallback` events by its place in
the list, names the same model. Retries are on by default, but the default
policy is left off the wire, so an unset `provider_retry` (and an empty
`fallback_models`) leave the spec's wire form, and so the digest, as it was
before they existed; `{"max_attempts": 1}`, retries off, changes it.

## Registrations

`Registration` (`registration.rs:19-21`) holds one undo closure:

- `dispose` takes the closure out and runs it. A second call finds nothing and
  does nothing (`registration.rs:38-48`).
- Dropping the handle does not undo anything. A caller that never disposes
  keeps the registration for the registry's lifetime (`registration.rs:5-6`,
  pinned by `dropping_does_not_undo`, `registration.rs:88-97`).
- `Registration::noop` is an already-undone handle (`registration.rs:31-34`).
  `is_disposed` says whether the undo has run (`registration.rs:51-56`).

Each undo removes only what its registration added. In `linch-core`, they come
from the tool registry, the hook chains and the agent's reloads (the reloads
build theirs with `Versions::push`, `agent/versions.rs:49`):

| Registry | Registration | Undo |
|---|---|---|
| `ToolRegistry::register` and `replace` | `registry.rs:86-97` | Removes registration `id` under the name. It bumps the generation only if that registration was the visible one, so disposing a hidden, replaced tool changes nothing (`registry.rs:141-149`, `251-271`) |
| `HookChain::register` | `hooks/chain.rs:132-180` | Removes the entry with this registration's id, only if it is still there. Registering the same hook twice gives two independent entries (`chain.rs:123-124`, `166-179`) |
| `Agent::reload_skills`, `reload_subagents` | `agent/reload.rs:39`, `64-86` | Brings back the set the agent had before, unless a later reload replaced it |

Both registries hold the undo's state behind a `Weak` pointer
(`registry.rs:141-143`, `hooks/chain.rs:166-169`), so a handle that outlives
its registry does nothing.

The agent disposes its own registrations when it is dropped: reloads first,
newest first, then the built-in tools (`agent.rs:202-217`). A build that
fails part-way disposes the built-in tools it had registered
(`builder.rs:326-355`).

## Hook chains

A hook runs at one point. `Point` fixes the point's request and response
types, so a hook can answer only what its point allows. It is sealed: only the
marker types in `linch-core` implement it (`hooks/hook.rs:38-61`). A `Hook<P>`
supplies a `policy_id` and `policy_version`, and may supply a tool matcher, a
timeout and a fail mode. The chain reads these once, at registration
(`hooks/hook.rs:63-99`). A hook that serves several points implements the
trait once per point.

`Hooks` holds one `HookChain` per point (`hooks.rs:53-70`) and is given to the
builder (`builder.rs:85-89`). A chain is shared across runs. Each `run` works
on a snapshot of the entries taken when it starts, so a registration made
meanwhile applies from the next run on (`hooks/chain.rs:61-65`, `188-191`).
`register` refuses a blank policy id, a matcher at a point that is not about a
tool, and a closed fail mode at `context_build` or `post_turn`
(`hooks/chain.rs:132-146`).

Each chain decides for itself how answers combine. A chain stops early only
where the table says so:

| Point | Rule | Fail mode by default |
|---|---|---|
| `user_prompt_submit` | A rewrite feeds the next hook. A block or a closed failure stops the chain and the run is never created (`hooks/turn.rs:83-105`, `runner/hooks.rs:81-115`) | open |
| `context_build` | Every hook runs. The blocks join in order; metadata merges with a later key winning. Cannot fail closed (`hooks/turn.rs:150-154`) | open |
| `pre_tool_use` | A rewrite feeds the next hook. A block, a resolve or a closed failure stops the chain. Only a chain that ran every matching hook lets the tool run (`hooks/tool.rs:101-136`) | closed |
| `permission_request` | The strictest answer wins. A deny or closed failure stops the chain; an allow does not (`hooks/tool.rs:226-258`) | closed |
| `post_tool_use` | A replacement feeds the next hook. A closed failure withholds the result and stops the chain (`hooks/tool.rs:312-336`) | open |
| `before_provider_call` | A rewrite feeds the next hook; the last rewrite is what is sent and what `context_build` records. A block or a closed failure stops the chain and ends the run as failed, before the provider is called (`hooks/call.rs`) | open, but its block can fail closed |
| `after_provider_call` | A rewrite feeds the next hook; the last rewrite is what gets staged and appended to the session. A retry stops the chain: the response (with any earlier rewrite) is accepted, its tool calls do not run, and the hook's feedback goes to the model before another turn. A block or a closed failure stops the chain and ends the run as failed, before the response is staged (`hooks/assembly.rs`). The call's receipt and usage are already durable when the chain runs ([turn lifecycle](./turn-lifecycle.md#a-crash-between-a-receipt-and-its-response)) | open, but its block can fail closed |
| `stop` | A rewrite feeds the next hook; the last rewrite is the run's final-answer text, not the assistant message already appended to the session. The first hook that keeps the run going (`Continue`, bounded only by `max_turns`) stops the chain; so does a `Retry` while the run call has its one retry left, after which a retry counts as an allow. A block or a closed failure fails the run (`hooks/turn.rs`). With an output schema it runs only on an answer that satisfies it, its request carries the parsed `structured_output`, and a rewrite is checked again: one that fails the schema fails the run | open |
| `post_turn` | Every hook runs; failures are only recorded. Cannot fail closed (`hooks/turn.rs:310-312`) | open |
| `agent_start`, `agent_stop`, `turn_start`, `provider_call_start`, `provider_call_stop`, `tool_use_start`, `tool_use_stop`, `post_tool_use_failure`, `pre_compact`, `post_compact`, `subagent_start`, `subagent_stop`, `event_emit` | Every hook runs; failures are only recorded. Cannot fail closed (`hooks/observe.rs`). `event_emit` fires once per event about to be delivered to the caller, but only one that is both streamed and durably logged: never for a hook's own record, and never for a stream-only delta or tool-progress event (`runner/hooks.rs`) | open |

The defaults are in `HookPoint::default_fail_mode` (`policy.rs:140-149`).
Failure handling is in `Entry::invoke` and `failed` (`hooks/chain.rs:208-259`):
a hook that errors or times out is skipped when open, and turned into the
point's blocking answer when closed.

Hooks are not barriers. Nothing is flushed around a chain, and a chain with no
hook is skipped before its request is built, so a run without hooks does no
hook work (`runner/hooks.rs:1-9`). Every call leaves a `HookRecord`, which the
run sends to the caller as a stream-only `hook` event at the last durable seq
(`runner/hooks.rs:41-79`).

## Tool call seams

The tool calls of one response go through two functions so a run can suspend
between them ([tools](./tools.md#how-a-batch-runs)).

`plan` (`scheduler/plan.rs:139-158`) decides every call and runs nothing. Per
call, in order (`plan.rs:168-206`):

1. Resolve the name in the turn's snapshot. An unknown tool is an error
   result.
2. Validate the input against the tool's schema.
3. Run the `pre_tool_use` chain (`plan.rs:212-254`), then validate the final
   input again. A `Resolve` answer becomes a tentative `Serve`.
4. Evaluate permissions, and on `Ask` run the `permission_request` chain
   (`plan.rs:258-293`). Then apply any recorded host answer: a recorded deny
   always denies; a recorded allow lifts an `Ask` only when the input is the
   one the host saw (`plan.rs:286-312`).

`execute` (`scheduler/execute.rs:120-153`) runs a decided plan and returns one
result per call, in call order. Around each tool it asks the `ToolSink`: the
tool starts only after `tool_starting` returns, and each result goes to
`tool_finished` as soon as it exists (`execute.rs:34-55`, `189-223`). A success
value is checked against the tool's `output_schema`, then, when the spec's
redaction covers tool results, `ExecuteOptions::redactor` scrubs every string
of the result, then the `post_tool_use` chain runs on the scrubbed result,
and a replacement it makes is scrubbed again (`execute.rs`, `finish`). With
that redactor set, progress reports are dropped. A tool that panics gets an error result
(`execute.rs:341-353`).

To add behavior to tool execution, use one of these:

- A `pre_tool_use` hook to rewrite, block or serve a call.
- A `post_tool_use` hook to replace or redact a result.
- A `Tool` wrapper registered with `ToolRegistry::replace`, which hides the
  wrapped tool and is undone with its `Registration`.
- `call_timeout_ms` and `max_tool_concurrency` in the spec for timeouts and
  the parallel limit (`agent/spec.rs:54-60`).

Do not edit `scheduler/` or `runner/` for a new gate. The spec's redaction
is the exception that proves the rule: it is not a hook because it must run
after every hook, even with no hook attached, and before the log, so it sits
at fixed places in the existing seams (`after_provider_call` and `stop` in
`runner/hooks.rs`, `finish` in `scheduler/execute.rs`, the prompt in
`submit`) rather than in a chain whose order the host controls.

## No globals, many agents

An `Agent` has no name lookup and no global state: every binding is a value
the host passes in (`agent.rs:6-8`). Per-agent state is in `AgentInner`:
registry, hooks, stores, subagent families and workers (`agent.rs:108-162`).
Per-run state is in the run's `Runner` (`runner/turn.rs:88-121`). The one
shared handle, from a worker tool back to its agent, is a per-agent
`OnceLock<Weak<AgentInner>>` set at the end of `build`
(`subagents/control.rs:14-18`, `builder.rs:250`, `320-321`). It is `Weak`, so
the registry that holds the tool does not keep the agent alive.

Tenants separate by namespace, not by process. An agent's namespace is part of
every store key, so two agents with different namespaces never share runs or
sessions even on one database (`builder.rs:103-107`,
[stores](./stores.md#keys-and-tenancy)). Many agents run on one Tokio runtime.
A run advances only while its stream is polled (`runner.rs:36-39`), and the
core spawns one kind of task: the one that drives the background subagents of
a suspended run (`runner.rs:38-39`).

A registry or hook chain that you add must follow the same rules: it lives in
`AgentInner` or on a value the builder takes, it hands out `Registration`s,
and the undo is identity-checked.

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
  counterpart. The extension points are seven typed hook chains, one per
  `HookPoint`, each with its own rule for how answers combine
  ([hook chains](#hook-chains)).
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
| `spec` | The serde `AgentSpec`: model, prompts, limits, compaction, budget (`agent/spec.rs:29-68`) |
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
`policy_version` (`hooks/hook.rs:76-78`).

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
| `stop` | The first hook that keeps the run going stops the chain. A closed failure fails the run (`hooks/turn.rs:246-250`) | open |
| `post_turn` | Every hook runs; failures are only recorded. Cannot fail closed (`hooks/turn.rs:310-312`) | open |

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
value is checked against the tool's `output_schema`, then the `post_tool_use`
chain runs (`execute.rs:310-338`). A tool that panics gets an error result
(`execute.rs:341-353`).

To add behavior to tool execution, use one of these:

- A `pre_tool_use` hook to rewrite, block or serve a call.
- A `post_tool_use` hook to replace or redact a result.
- A `Tool` wrapper registered with `ToolRegistry::replace`, which hides the
  wrapped tool and is undone with its `Registration`.
- `call_timeout_ms` and `max_tool_concurrency` in the spec for timeouts and
  the parallel limit (`agent/spec.rs:53-59`).

Do not edit `scheduler/` or `runner/` for a new gate.

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

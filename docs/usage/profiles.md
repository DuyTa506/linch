# Domain profiles

Use `Profile` when runs on one agent need different versioned roles and configuration.

[← Usage guide](./README.md)

Use `Profile` to select a versioned role and configuration for each run on an
existing `Agent`. Support and billing conversations can share the agent's
provider and stores while using different instructions and tools:

```python
import asyncio
from linch import Agent, InMemorySessionStore, Profile, RunOptions, empty_tools

# support_search and billing_lookup are your existing tool implementations.
agent = Agent(
    model="gpt-5",
    session_store=InMemorySessionStore(),
    tools=empty_tools(support_search, billing_lookup),
)
base = Profile(id="base", version="1", max_turns=8)
support = base.layer(Profile(
    id="support", version="2",
    system_prompt="Help customers resolve product issues.",
    tool_allow=(support_search.name,),
))
billing = base.layer(Profile(
    id="billing", version="1",
    system_prompt="Explain invoices using the billing lookup results.",
    tool_allow=(billing_lookup.name,),
    budget={"max_tokens": 20_000},
))

async def answer(profile, question):
    session = await agent.session()
    return [event async for event in session.run(question, RunOptions(profile=profile))]

support_events, billing_events = await asyncio.gather(
    answer(support, "How do I reset access?"),
    answer(billing, "Where is my invoice?"),
)
```

Each conversation needs its own session; a session still admits one active run.
The profile's instructions append to the host's instructions. Profile tools
intersect an existing session tool override, and the normal permission engine,
hooks, and offered-tool boundary remain authoritative. An allow list never
grants permission denied by the host. `tool_allow=()` offers no tools, while
`tool_deny` removes names even when also allowed.

## Layers and inspection

`base.layer(domain).layer(tenant_patch)` produces a new frozen profile. A later
non-`None` field replaces the earlier field; `None` inherits it. Plugin rows
merge by their stable mapping key: replacing a row keeps its position, and new
rows append. The final layer supplies the effective id and version. Empty tool
tuples replace a tool list; budget and schema mappings replace the whole field.
An output schema must supply valid `name` and `schema` arguments.
`profile.resolve()` returns a detached JSON-safe dictionary suitable for
inspection or configuration dumps.

Plugins use the existing `Plugin` protocol and `agent.use` lifecycle:

```python
support = support.layer(Profile(
    id="support-acme", version="3",
    plugins={"knowledge": (knowledge_plugin, {"namespace": "acme-support"})},
    output_schema={
        "name": "answer",
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    },
))
print(support.resolve())
```

Plugin configurations, output schemas, and budget limits are declarative data;
their nested mappings and sequences are frozen. Plugins remain host-supplied
implementations. Live dependencies belong in agent `Context` services or
`RunOptions(deps=...)`, rather than in profile configuration. Bump a plugin's
version when its implementation changes.

Ordinary runs dispose their plugin scopes when iteration finishes or closes.
Detached work keeps the scope alive until its last task completes. Retained
children keep their services for continuation until their sessions are released;
releasing the last child also disposes the scope. `agent.close()` remains the
fallback for draining work and retrying failed disposal.

`output_schema` contains the existing `OutputSchema` constructor arguments.
`budget` contains `RunBudget` limit arguments (`max_tokens`, `max_cost_usd`,
`warn_ratio`); each run gets a fresh accumulator. Explicit `RunOptions` schema
or budget overrides the profile default. `max_turns` sets the existing loop
limit. Profiles do not introduce another execution or structured-output engine.

## Resume and rollout

There is no mutable profile registry. Direct callers supply the profile
definition on launch and resume. Keep released definitions available in the
host application for as long as their runs may resume:

```python
async for event in session.resume(run_id, RunOptions(profile=support)):
    ...
```

At launch the RunContract stores `id`, `version`, and `content_hash` under
`payload.run_options.profile` inside `RunRecord.meta["run_contract"]`. The hash
covers the composed configuration, ordered plugin rows, plugin identities, and
configs. Resume and lease takeover compose the supplied definition again and
compare the whole contract before provider or tool execution. Missing profiles,
different versions, and changed content under the same version are rejected.
Rolling out a new profile affects new runs; existing runs must use their pinned
definition. `profile=None` preserves the existing request and contract shape.

## Queued runs and worker takeover

Pass the selected profile and principal when enqueueing, and supply the released
definitions to each worker:

```python
from linch import Principal
from linch.worker import RunRequest, Worker, enqueue_run, enqueue_runs

principal = Principal(tenant="acme", user="alice", roles={"support"})
run_id = await enqueue_run(pool, session_id, "Reset my access",
                           profile=support, principal=principal)
await enqueue_runs(pool, [RunRequest(other_session_id, "Find my invoice",
                                    profile=billing, principal=principal)])
worker = Worker(pool, agent_factory, profiles=(support, billing))
await worker.run()
```

Pending run metadata and job payloads contain only the profile's
`{id, version, content_hash}` reference and the serialized principal; plugin
objects and profile definitions stay in the host process. The worker resolves
the exact reference before execution and passes the definition through
`RunOptions`. Missing definitions or mismatched content fail the run with an
actionable `ConfigError`. Once launch writes a RunContract, its pinned profile
and principal take precedence on resume and lease takeover. Every worker that
may reclaim the run must retain that released definition. See
[production wiring](./production.md#scale-out) for the worker factory.

`linch.ext.deep_agent.DeepAgentProfile` stays separate: it configures factory-time
durability, coordinator behavior, and worker retention. Domain profiles select
per-run configuration on the resulting agent.

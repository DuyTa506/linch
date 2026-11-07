# Skills and subagents

> Part of the [linch-rust architecture guide](./README.md).

Skills and subagents are built-in tools of `linch-core`. The `Skill` tool
returns a skill's prompt to the model (`crates/linch-core/src/skills.rs`,
`skills/`); the `Subagent` tool runs a task as a durable child run
(`crates/linch-core/src/subagents.rs`, `subagents/`), in the foreground or in
the background, and the worker tools address the children it keeps. The
children of a run live in its family (`crates/linch-core/src/runner/family.rs`).
Use this page when you change these tools, how child runs start, resume or
end, or how a reload replaces skills and subagents.

Python Linch has the same tools ([skills and subagents](../../../docs/architecture/skills-subagents.md));
[where Rust differs](#where-rust-differs-from-python) is at the end. The
usage pages ([skills](../usage/skills.md), [subagents](../usage/subagents.md))
show them from the Python client.

## Skills

`AgentBuilder::skills` (`crates/linch-core/src/agent/builder.rs:152`) turns
skills on with the host's `Skill` values (`crates/linch-core/src/skills.rs:49`);
the `linch` facade loads them from `.linch/skills/*/SKILL.md`. At build
(`builder.rs:235-245`):

- the built-in `verify` skill joins them unless one has its name,
  case-insensitively, and the set is sorted by lowercase name
  (`skills.rs:151-158`);
- every skill, serialized, enters the run contract as the `skills` feature;
- the `Skill` tool is registered for as long as the agent lives;
- the listing goes into the system prompt as a cacheable block after the
  spec's prompt, so it is part of each new session's frozen prefix.

A bare agent has no skills and no `Skill` tool.

### The listing

`listing` (`crates/linch-core/src/skills/listing.rs:25`) writes one
`- name: description` line per skill the model may invoke (not those with
`disable_model_invocation`), sorted by lowercase name. A description is the
skill's `description`, then ` — when_to_use` and ` (args: argument_hint)`
when set, cut at 250 characters. The listing may take 1% of the provider's
context window at four characters a token (5120 characters for 128000;
8000 without a window), as Python's budget does. Past that, every
description is cut to an equal share; under a 20-character share the
listing keeps the names only (`listing.rs:5-14`, `listing.rs:26-29`). The block reads
"The following skills are available for use with the Skill tool:"
(`crates/linch-core/src/skills/set.rs:93-100`). When tool search defers the
skills, a new session's block names them only (`set.rs:87-91`).

### The `Skill` tool

`SkillTool` (`crates/linch-core/src/skills/tool.rs:21`):

| Property | Value |
|---|---|
| Input | `skill` (required; a leading `/` is dropped), `args` |
| Scope | `Read`: it runs without approval and next to other read calls; a rule with a `skill` condition can still deny or ask (`tool.rs:18-20`) |
| `always_load` | `true`: tool search never defers it (`tool.rs:78`) |
| Result | The skill's body with its placeholders filled (`substitute`, `crates/linch-core/src/skills/substitute.rs:15`): `$ARGUMENTS`, `${SKILL_DIR}`, `${LINCH_SKILL_DIR}`, `${LINCH_SESSION_ID}` and each named argument, in one pass, with no shell quoting |
| Errors | `Unknown skill: <name>`; `Skill is not invokable by the model: <name>` (`tool.rs:94-105`) |
| Events | `skill_invoked` `{name, args}` just before the call's `tool_call_start`, `skill_completed` `{name, is_error}` just after its `tool_call_end`, as in Python. The run's tool sink stages them with the start and the end, so they ride those writes (`crates/linch-core/src/runner/sink.rs:102-131` and 173-180). A call that is denied, blocked or served gets them too; an unknown tool or invalid input does not (`skill_name`, `sink.rs:104-108`) |

The tool reads the agent's current skill set on every call, so a
[reload](#catalog-and-reload) takes effect at once.

A loaded body stays in the history as the tool result. When a compaction
folds that result away, the body comes back as a `<system-reminder>` in the
summary message, which is a logged projection: the provider view gets it,
the full history does not (`skills.rs:11-14`, `skills.rs:165-193`).

### The overlay

A skill whose frontmatter names `allowed_tools` or `model` sets them for
the model turn right after it loads, and only that turn (`skills.rs:16-21`):

1. `overlays` (`crates/linch-core/src/skills/overlay.rs:27`) keeps, per
   skill, a non-empty `allowed_tools` and a non-empty `model`.
2. At each turn, `Runner::narrow` (`crates/linch-core/src/runner/turn.rs:159-175`)
   looks at the provider view: the last skill with an overlay among the
   successful `Skill` calls of the assistant message just before the last
   user message (`overlay.rs:45-69`).
3. Its `allowed_tools` filter the turn's tool snapshot: the request offers
   only those, and a call of another is refused as not registered. Its
   `model` replaces the request's model, with the run model's `[1m]`
   suffix added when the run model has it (`model_override`, `overlay.rs:73`;
   `turn.rs:272-284`).

Nothing is stored for the overlay; a resumed turn reads the one it was
dispatched with from the same view (`overlay.rs:1-5`). The overlay only
narrows: permission rules still decide every call. The turn's
`context_build` event names the model it requests
(`crates/linch-core/src/events/kinds/control.rs:44-47`). The `skill_invoked`
fields `model_override` and `allowed_tools` stay `None`: the tool runs
before the turn the overlay applies to, and the overlay is not computed by
the tool.

### `skills_loaded`

A new run stages `skills_loaded` when its agent has skills, the run offers
the `Skill` tool (a child whose definition leaves it out does not), and its
session's log is empty, between `init` and the prompt's `user` event, so it
rides the create write (`crates/linch-core/src/runner/start.rs:97-105`). It holds one
descriptor per skill, sorted by name: `name`, `description`, and
`when_to_use` and `argument_hint` when set (`set.rs:55-78`). The rule is
durable: a session's later runs never send it, after a restart either.

## Subagents

`AgentBuilder::subagents` (`builder.rs:164`) turns subagents on with the
host's `SubagentDefinition`s (`crates/linch-core/src/subagents.rs:144`);
the facade loads them from `.linch/agents/*.md`. The `Roster`
(`subagents.rs:234-276`) adds the built-in `verification` agent unless one
has its name, and keeps a general-purpose agent, `_default`, for a call
with no `subagent_type` (or a blank one, `general-purpose` or `_default`).
The roster enters the contract as the `subagents` feature, and the
`Workers` setting, when given, as `workers`.

The build registers (`builder.rs:261-277`):

| Tool | When | Scope |
|---|---|---|
| `Subagent` | subagents on | `Exec` |
| `SubagentContinue` | `workers.background` or `workers.retain` | `Exec` |
| `TaskStop` | `workers.background` | `Exec` |

`workers` without `subagents` fails the build (`builder.rs:252-254`).

### The `Subagent` tool

`SubagentTool` (`crates/linch-core/src/subagents/tool.rs:32`) takes
`description` (non-blank), `prompt` (non-empty), `subagent_type` and
`run_in_background`. Its description lists the roster's types.

It is exec-scoped, so it asks in permission mode `default`. Its `parallel`
predicate (`tool.rs:124-146`) lets a call run next to other read calls only
when it is in the foreground and names a type whose definition lists its
tools, each of them a registered read-scope tool; `*`, an unknown type or
tool, or no list fails closed.

A call (`tool.rs:167-212`):

1. resolves the definition, or answers with the types it knows;
2. refuses `run_in_background` unless `workers.background` is on;
3. names the child run, and its session, after the call's idempotency key,
   `<parent run id>:<tool_use_id>` (`tool.rs:192`;
   `crates/linch-core/src/tool.rs:183-190`);
4. enlists the worker in the agent's worker registry;
5. runs the child in the background (see [workers](#background-workers))
   or in the foreground. A foreground child that nothing can address again
   has its session closed at once; a retained one's result ends with
   `[Worker ID: <id>]`.

### The child agent

`AgentInner::child` (`crates/linch-core/src/agent/child.rs:109-153`) builds
the agent a child run runs with:

- the parent's spec, provider, permission engine, hooks, stores and
  namespace, skills and mailbox;
- the parent's tools through `ChildTools` (`child.rs:15-40`): never
  `Subagent`, `SubagentContinue`, `TaskStop` or `tool_search`, and only the
  named ones when the definition names any (`*` names all);
- the parent's system blocks (the spec's `system_blocks`, then its system
  prompt), the skill listing when the child keeps the `Skill` tool, then
  `User-provided instructions:` and the definition's body;
- no subagents, so a child cannot spawn; and `child: true`, so a call that
  would ask is denied instead (`crates/linch-core/src/runner/batch.rs:23-24`,
  `batch.rs:73-75`), unless a rule or a `permission_request` hook allows it.

**The child's contract comes from the parent's frozen build.** It is
digested from the tools and hook policies the parent's own contract was
built from (`built_tools`, `built_hooks`), filtered, plus the definition as
the `subagent` feature (`child.rs:96-123`). A tool registered on the parent
since does not change it, so a child resumes across registrations; its runs
still use the live tools, as the parent's do. A host can get the same agent
with `Agent::subagent` and start its runs under a parent with
`Agent::run_child` (`child.rs:42-83`); the host then drives and ends them.

### A foreground child

`subagents::run::run` (`crates/linch-core/src/subagents/run.rs:27-76`)
loads the child run first:

| Found | Then |
|---|---|
| Nothing | Start it with the parent's run as `parent_run_id`, the call's principal and trace id, and a child token of the call's cancel token |
| A terminal run | Read its end back from its events (`run.rs:136-145`) |
| A run of another contract | Fail it for good, so its session takes runs again, and give the parent the `ContractMismatch` (`run.rs:113-133`) |
| A live run | Resume it |

When the child run exists, `first_use` (`run.rs:78-111`) first refuses a call
whose id an earlier response of the run already used: the parent's log
holds events of that child before its last response, so the provider
repeated a tool-use id.

The parent's family adopts the child, and the call waits for it. Each
envelope of the child's stream is wrapped in a `subagent` event and handed
to the parent (`run.rs:147-164`); see [subagent wrapping](./events.md#subagent-wrapping).
Stream-only events reach the parent's stream at once and durable ones with
the call's end, so on the parent's stream a child's `partial_assistant` can
come before its `init`. The child's end becomes the result
(`run.rs:195-224`): its final text, or an error that keeps the partial
output (`Subagent aborted.`, `Subagent encountered an error: ...`).

A call that its timeout or the run's cancel cuts short cancels the child's
token and leaves the child to write its abort; the parent waits for every
child before its terminal write (`subagents.rs:28-31`).

The parent's `total_usage` counts its own model calls only; a child's usage
reaches the parent wrapped, as in Python
(`crates/linch-core/tests/e2e_subagents.rs:70-73`).

## Background workers

`Workers` (`subagents.rs:100-131`) has three settings, all in the contract:

| Field | Default | Meaning |
|---|---|---|
| `background` | `false` | A call may set `run_in_background` |
| `retain` | `false` | A foreground worker stays addressable after it answered |
| `max_background` | 4 | Background children one run may have running; a call past it gets a tool error. Python has no cap |

A background call (`crates/linch-core/src/subagents/background.rs:96-115`)
never runs in parallel. It hands the child to the family and answers at once
with `Worker '<id>' started in background...`, after a `child_run_status`
event (`running`) that rides the call's end. The cap is checked only when a
call starts a new child (`background.rs:148-159`); a resumed run's children
were admitted when they began.

The child goes on next to the parent, driven by the parent's stream:

- its durable envelopes reach the parent's log wrapped, at the parent's next
  turn (`background.rs:279-292`);
- its end becomes a `Completion` in the family's inbox. At the end of the
  parent's next tool batch, `Runner::notes` (`crates/linch-core/src/runner/inbox.rs:50-70`)
  stages a `child_run_status` event of the end and adds a
  `<task-notification>` (Python's text, escaped as Python's `html.escape`,
  `background.rs:55-91`) to the logged tool results;
- when the parent answers with text while a background child runs,
  `Runner::hear_back` (`inbox.rs:72-109`) does not end the run: it waits for
  the next end and logs the notification as a `user` message of subtype
  `notification`, so the model takes another turn;
- a parent that ends anyway (cancelled, failed, out of turns) cancels its
  children, waits for their aborts, and records each end it did not report
  in its terminal write (`crates/linch-core/src/runner/end.rs:19-42`).

`SubagentContinue` (`crates/linch-core/src/subagents/control.rs:74-166`)
resolves a worker of the calling session by id or unique display name
(`crates/linch-core/src/subagents/workers.rs:74-105`), refuses one that
still runs, and runs it again on its own session, history intact, as a new
foreground child run of the calling run, named after the continue call.
`TaskStop` (`control.rs:168-252`) cancels a running background worker and
marks it stopped, so its end is recorded with no notification; the worker
stays addressable.

The worker registry (`workers.rs:26-130`) keeps, per parent session and in
process memory, each worker's display name (its definition's name, or
`Agent #N` for the N-th general-purpose worker) and whether it is
addressable. `Agent::close_session` (`crates/linch-core/src/agent.rs:329-345`)
closes the session's suspended families, its workers' sessions, then the
session.

## The run family

A run's children are its `Family` (`crates/linch-core/src/runner/family.rs:44`).
The family lives on the agent, listed by run id (`family.rs:370-401`), from
the run's create or claim until its stream lets go of it
(`crates/linch-core/src/runner/start.rs:333-345`). That is what lets it
outlive a stream that ends with the run suspended.

- **While a stream drives the run, the core spawns no task.**
  `RunStream::poll_next` polls the family's children next to the run future
  (`crates/linch-core/src/runner.rs:128-156`), so children advance only while
  the caller polls.
- **Suspend:** before the `AwaitingApproval` write, the runner parks the
  family; a failed write unparks it (`crates/linch-core/src/runner/batch.rs:141-180`).
- **The keeper:** when the stream of a parked run stops, its children go to
  one task, spawned on the caller's Tokio runtime, which drives them until a
  stream of the run attaches again or none is left (`family.rs:230-268`).
  Without a runtime they wait, unpolled, for that stream. This is the one
  kind of task the core spawns.
- **Take-over:** an approve or a resume in this process attaches a new
  stream to the same family; the keeper sees the driver change and exits
  (`family.rs:180-211`). The children never went through a crash.
- **Close:** closing the run's session or dropping the agent cancels a
  parked family's children; the keeper drives them until their aborts are
  durable (`family.rs:270-290`; `agent.rs:202-205`).
- **Any other stop** (a dropped stream, a failed run) drops the children
  with the family, like a crash.

`Agent::parked_children` (`agent.rs:348-368`) gives a future that resolves
once a suspended run's children are done. The RPC server uses it to keep a
suspended run's place under `max_runs` while its workers run
([RPC](./rpc.md#limits)).

## Resume and reattach after a crash

A child run is an ordinary durable run, so a crash leaves it at its last
durable state like its parent. Resuming the parent picks the children up:

- **Foreground:** a `Subagent` or `SubagentContinue` call with a durable
  start and no durable end is planned again instead of being reported as
  interrupted (`rerun_subagents`, `batch.rs:118-139`). The call runs again
  and finds its child run: it resumes it (the child's journal never pays a
  model call twice), reads back one that finished, or fails one whose
  contract changed.
- **Background:** after the claim, `reattach`
  (`crates/linch-core/src/subagents/background.rs:184-252`, called at
  `start.rs:313-315`) enlists the run's workers again from its
  `tool_call_start` events, in call order, then takes each child that a
  `child_run_status` marks `running` with no later end, and that the family
  does not hold already: it resumes it, starts it (its create never landed),
  or reads its end back. One that cannot be picked up reports a failed end.
- **Suspended in this process:** the family is still listed, so the resume
  or approve takes it over as it is (`start.rs:206-208`).

The children's contracts come from the parent's build, so a resume after a
registration on the parent still matches; a definition changed by a reload
does not, and that child ends `failed`. With memory stores everything is
lost with the process.

## Catalog and reload

Both reloads are reversible registrations built on `Versions`
(`crates/linch-core/src/agent/versions.rs:8-13`): a stack of values whose
last one is current. A push (`versions.rs:47-75`) returns a `Registration`; disposing it removes
exactly that value, so a later push stays current, and disposing twice does
nothing. The run contract stays the one the agent was built with, so runs
resume across reloads (`crates/linch-core/src/agent/reload.rs:1-9`).

| | `Agent::reload_skills` (`reload.rs:20-44`) | `Agent::reload_subagents` (`reload.rs:46-87`) |
|---|---|---|
| Takes | The new skills; `verify` joins as at build | The new definitions |
| Replaces | The current `SkillSet`; the `Skill` tool reads it per call | The roster, and the `Subagent` tool, through `ToolRegistry::replace`, since its description lists the roster |
| Dispose | Brings back the earlier set | Brings back the earlier tool and roster. The agent keeps the registration and disposes it when it drops, so the replaced tool never outlives the agent in the registry it shares (`agent.rs:206-213`) |
| Refused | `BuildError::Invalid` when built without skills | `BuildError::Invalid` when built without subagents |

A session that already has a frozen prefix keeps it byte for byte and is
told of the change in a catalog notice at the tail of its next logged user
message; only a new session takes the live catalog into its prefix
(`crates/linch-core/src/catalog.rs:1-22`). For subagents the tool's schema
is unchanged, so the notice says the tool was updated.

The facade's `OpenedAgent::reload_skills` and `reload_subagents`
(`crates/linch/src/open/reload.rs:10-59`) scan the config directory again,
off the async loop, only for a kind the config turned on, add the deep
agent's roster again, and return the entries the scan left out. Neither
reload is an RPC method or a Python client call at this revision.

### The deep agent roster

`linch-ext` holds the deep agent preset. `deep_agent_subagents`
(`crates/linch-ext/src/deep_agent/subagents.rs:7-12`) is Python's roster:
`researcher`, `planner`, `implementer` and a read-only `verification`, which
takes the place of the core's built-in one (that one has `Bash`).
`DeepAgent::subagents` (`crates/linch-ext/src/deep_agent.rs:122-135`) puts the
host's definitions first and adds each roster entry whose name none of them
has, case-insensitively. The profile's `workers`
(`crates/linch-ext/src/deep_agent/profile.rs:79-86`) set `background` and
`retain` from the profile's flags, with the default `max_background`.

The facade turns the preset on from `AgentConfig.deep_agent`: `deep::expand`
(`crates/linch/src/deep.rs:10-33`) sets tasks, the virtual filesystem, the
workers (unless the config sets them) and the spec; `deep::add_roster`
(`deep.rs:35-45`) turns skills on, so the built-in `verify` skill joins, and
adds the roster after the config directory's subagents, at open
(`crates/linch/src/open.rs:156`) and at each reload. Two parts of Python's preset have no kernel support yet
(`deep_agent.rs:33-39`): a coordinator keeps the workspace tools
(`COORDINATOR_EXCLUDED_TOOLS` is only named), and the profile's
`max_tokens` is not enforced.

## Example

This compiles and runs against `linch-core` at this revision. A skill
narrows the next turn to `Subagent` on a small model, the subagent runs as
a child run, and a reload of the skills is undone:

```rust
use std::sync::Arc;

use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Envelope, Event, MemoryRunStore, MemorySessionStore,
    PermissionEngine, PermissionMode, PermissionPolicy, RunOptions, ScriptedProvider, Skill,
    SubagentDefinition,
};
use serde_json::json;

async fn drain(agent: &Agent, session: &str, prompt: &str) -> Vec<Envelope> {
    let stream = agent.run(prompt, RunOptions::new(session));
    stream.map(|item| item.expect("the run continues")).collect().await
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    // `scan` narrows the turn after it loads to `Subagent` on a small model.
    let mut scan = Skill::new("scan", "/skills/scan", "Survey the repo.", "Delegate a survey.");
    scan.allowed_tools = Some(vec!["Subagent".into()]);
    scan.model = Some("small-model".into());
    // `scout` names its tools: none, so the child gets none.
    let mut scout = SubagentDefinition::new("scout", "Reads around.", "Report what you find.");
    scout.tools = Some(vec![]);

    // Parent and child share the provider, so turns go in call order.
    let spawn = json!({"description": "Scout", "prompt": "Look around.", "subagent_type": "scout"});
    let provider = Arc::new(ScriptedProvider::new([
        TurnBuilder::new("m").tool_call("c1", "Skill", &json!({"skill": "scan"})).build(),
        TurnBuilder::new("m").tool_call("c2", "Subagent", &spawn).build(),
        TurnBuilder::new("m").text("found 3 files").build(), // the child
        TurnBuilder::new("m").text("done").build(),
        TurnBuilder::new("m").text("again").build(),
        TurnBuilder::new("m").text("back").build(),
    ]));
    let permissions =
        PermissionEngine::new(PermissionPolicy::new(PermissionMode::SkipDangerous, "/")).unwrap();
    let agent = AgentBuilder::new(AgentSpec::new("m"))
        .provider(provider.clone())
        .permissions(permissions)
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("docs")
        .skills(vec![scan])
        .subagents(vec![scout])
        .build()
        .unwrap();

    let events = drain(&agent, "s1", "survey").await;
    let run_id = events[0].run_id.clone();
    let requests = provider.requests();
    let tools = |at: usize| -> Vec<String> {
        requests[at].tools.iter().map(|tool| tool.name.clone()).collect()
    };
    assert_eq!(tools(0), ["Skill", "Subagent"]);
    assert_eq!((tools(1), requests[1].model.as_str()), (vec!["Subagent".into()], "small-model"));
    assert!(tools(2).is_empty(), "the child's request");
    assert!(requests[2].system.last().unwrap().text.starts_with("User-provided instructions:"));

    // The child's events are wrapped; its run id is the call's key.
    let child = format!("{run_id}:c2");
    let init = events.iter().find_map(|envelope| match &envelope.event {
        Event::Subagent(sub) if sub.child_run_id.as_str() == child => match &*sub.event {
            Event::Init(init) => Some(init),
            _ => None,
        },
        _ => None,
    });
    assert_eq!(init.unwrap().parent_run_id.as_ref(), Some(&run_id));
    let answer = events.iter().find_map(|envelope| match &envelope.event {
        Event::ToolCallEnd(end) if end.tool_use_id() == "c2" => Some(json!(end.result)),
        _ => None,
    });
    assert_eq!(answer.unwrap()["content"][0]["text"], "found 3 files");
    let Event::Result(result) = &events.last().unwrap().event else {
        panic!("the run ends with its result");
    };
    assert_eq!(result.final_text.as_deref(), Some("done"));

    // A reload is a registration: new sessions list `lint` until it is
    // disposed, then `scan` again.
    let listing = |at: usize| provider.requests()[at].system.last().unwrap().text.clone();
    let lint = Skill::new("lint", "/skills/lint", "Lint the code.", "Run the linter.");
    let reload = agent.reload_skills(vec![lint]).unwrap();
    drain(&agent, "s2", "hi").await;
    assert!(listing(4).contains("- lint: Lint the code."));
    reload.dispose();
    reload.dispose(); // idempotent
    drain(&agent, "s3", "hi").await;
    assert!(listing(5).contains("- scan: Survey the repo."));
}
```

## Where Rust differs from Python

| Python Linch | linch-rust |
|---|---|
| An invoked skill's body is put back as a `<system-reminder>` every turn | The body is the `Skill` tool's result; it comes back as a reminder only after a compaction folds the result away |
| The `Skill` tool sets a pending overlay (`allowed_tools`, `model`) on the session, kept in checkpoints | The overlay is read from the provider view at each turn; nothing is stored |
| `skill_invoked` before the call's `tool_call_start`, `skill_completed` after its `tool_call_end` | The same |
| `skills_loaded` once per session object (`skills_loaded_emitted`), so again after a restart, whenever the agent has skills | Once per session, on its first run (its log is empty), and only when the run offers the `Skill` tool |
| `reload_skills()` and `reload_subagents()` rescan the project | The core takes the new lists; the facade rescans; neither is on the wire yet |
| A fresh child's system blocks are built from its tool names; a fork reuses the parent's prefix | A child gets the parent's system blocks and prompt, the listing if it has `Skill`, then its body. Forking is `Agent::fork_session`, not a subagent mode |
| A background worker is an `asyncio` task: it can outlive the run, and a restart cannot resume it | A child is a durable run the parent's stream drives; the parent waits for its workers before it ends, and resumes them with it on Postgres |
| No cap on background workers | `max_background`, 4 by default |
| Worker ids like `agent-a1b2` | `<parent run id>:<tool_use_id>` |
| Notifications drained at the top of the next turn, optionally from a durable inbox | Ends are recorded as `child_run_status` events and the notification joins the parent's next logged user message |
| A `RunBudget` object on the agent config (or the run options), shared by every run that uses it and inherited by the subagent tree | A spec `budget` gives each root run its own meter (`crates/linch-core/src/budget.rs`), which the run's family hands to every child; a resumed root run sums its own and its children's durable receipts. A parent's totals still count its own calls, as Python's `ResultEvent` does |
| A coordinator loses its heavy tools | Not applied: the kernel offers a parent every tool its subagents can use |

## Tests

| Test | Pins |
|---|---|
| `crates/linch-core/tests/e2e_skills.rs` | The listing in the cached prefix, the tool's body, the contract, the reminder after compaction |
| `crates/linch-core/tests/e2e_skill_overlay.rs` | Skill events, the overlay's tools and model, across a crash |
| `crates/linch-core/tests/e2e_subagents.rs` | Foreground children: parent id, fresh session, filtered tools, wrapping, parallel read-only types, usage totals |
| `crates/linch-core/tests/e2e_background.rs`, `e2e_workers.rs` | Background children, notifications, `hear_back`, continue, stop, session close |
| `crates/linch-core/tests/e2e_background_suspend.rs` | Children of a suspended parent keep running and are taken over |
| `crates/linch-core/tests/e2e_subagent_crash.rs`, `e2e_background_crash.rs` | A crash at every write, then a resume that picks the child up |
| `crates/linch-core/tests/e2e_child_contract.rs`, `e2e_child.rs` | Child contracts from the parent's build; host-driven child runs |
| `crates/linch-core/tests/e2e_reload.rs` | Reloads as registrations, prefixes kept, catalog notices |
| `crates/linch-core/tests/e2e_subagent_cleanup.rs` | A cut-short call leaves its child aborted before the parent ends |
| `crates/linch-core/tests/e2e_budget.rs` | A child's spend counts against its parent's budget, an exhausted tree refuses a child's first call, a resumed tree keeps its children's spend |

---

Back to the [architecture index](./README.md).

# Profile runs as configuration overlays

Status: **proposal for the architecture owner's phase 2 review**. Phase 1 changes
only the four kernel/constructor defects (82-1, 82-2, 82-3, 87-3). None of the
overlay behavior below is implemented by this document.

The owner decision is to remove the shallow Agent copy in `_profile_scope`.
`session.agent` always identifies the real Agent, including child sessions,
profile plugins, background work, resume, and shutdown. A profile changes run
configuration and owns effects; it does not become another Agent.

## Inventory and audit method

References below are relative to `src/linch/`. The inventory includes direct
`session.agent` reads, aliases (`agent = session.agent`, `ctx.agent` in loop
contexts, `args.parent_agent`, and Agent methods' `self`), and string-based
`getattr` reads. Constructor-only assignments are distinguished from run-time
consumers. Counts describe unique **source-line access sites**, not execution
frequency or AST-load counts; a line reading one field twice counts once for
that field. Categories overlap (a Context identity comparison is also a Context
read). References are against phase 1 source at `801eab6`; the profile
implementation is still the copy-and-swap code from `163922f`. Profile
dataclass validation reads and unrelated objects' same-named fields are excluded
from Agent-field totals and listed separately where useful.

### Identity and Context

| Category | Count | Exact sites |
|---|---:|---|
| RAG Session-Agent identity | 6 | `rag/__init__.py:81,85,102,107,111,175` |
| ToolSearch Session-Agent identity | 1 | `tools/search.py:191` |
| Session ownership / copied-child identity | 4 | `agent.py:1747`; `sessions/fork.py:89`; `profiles.py:247,257` |
| Session-Agent Context identity | 1 | `loop/reliability.py:99` |
| **All Session/child-Agent identity comparisons** | **12** | 11 compare the Agent directly; one compares its Context. No `==` / `!=` comparisons. |

Eight sites are extension guards (six RAG, ToolSearch, reliability); the other
four control ownership/disposal. RAG's additional `hook.agent is not self.agent`
at `rag/__init__.py:174` is not a Session-Agent read. ToolSearch's service
identity guard is `tools/search.py:199`. Worker checks at
`worker/runtime.py:384,386` compare stores, not Session-Agent identities.
There are no Agent identity comparisons in `ext/`; workflow passes its Agent
as `parent_agent` instead.

There are **3 direct `session.agent.context` reads**:
`loop/runner.py:585`, `loop/runtime.py:245`, `loop/reliability.py:99`.
Including aliases, Agent `self`, and `getattr`, the Context inventory has
**27 sites**:

| File | Exact lines | Count | Concern |
|---|---|---:|---|
| `agent.py` | `746,748,750,751,798,807,837,1906` | 8 | Services/effects, listener API, mailbox, mount, teardown |
| `_telemetry.py` | `163` | 1 | Agent-owned telemetry cache |
| `loop/request.py` | `67,196` | 2 | Request catalog |
| `loop/runner.py` | `585` | 1 | Inbox fast path |
| `loop/runtime.py` | `245` | 1 | Inbox drain |
| `loop/startup.py` | `55` | 1 | Catalog skill listing |
| `loop/reliability.py` | `21,93,95,99` | 4 | Run index, captured Context, registration, identity guard |
| `profiles.py` | `177` | 1 | Child scope creation |
| `rag/__init__.py` | `135,158,170,240,248` | 5 | RAG services and effects |
| `tools/search.py` | `199` | 1 | Terminal catalog guard |
| `scheduler.py` | `1619` | 1 | Event bus via `getattr(agent, "context")` |
| `kernel/context.py` | `221` | 1 | Inbox target through resolved Agent |

`profiles.py:178` assigns the copied Context and is a write, not a read.
`loop/reliability.py:99` also reads the hook's saved `self.context`; that is
the same guard site, not another Agent-Context access site.

### Requested configuration fields

The **100 field access sites** below include run consumers, plugin registration,
prompt building, extension bootstrap, mid-run host mutation and teardown.
Counts are per field: different fields on one line count in both rows.
`agent.tools.register()` reads the registry attribute even though the method
mutates it. Direct field assignment alone is excluded.

| Agent field | Count | Exact access sites |
|---|---:|---|
| `tools` | 39 | `loop/contracts.py:46`; `loop/request.py:262`; `loop/runtime.py:581`; `loop/startup.py:128,146`; `scheduler.py:1618`; `subagents/runner.py:224`; `subagents/generator.py:175`; `hooks/adapters.py:68`; `tools/search.py:86,97,99,102,281,296`; `coordination/mailbox/__init__.py:41,44`; `tools/ask_user.py:124`; `agent.py:657,658,855,927,946,947,993,1273,1295,1310,1381,1392,1398,1406,1425,1426,1427,1476`; `profiles.py:183,202,203` |
| `hooks` / `_hooks` | 21 | `loop/runner.py:179,186`; `loop/request.py:109`; `loop/contracts.py:156`; `loop/runtime.py:617`; `scheduler.py:846`; `session.py:424`; `subagents/runner.py:291,389`; `compaction.py:819`; `rag/__init__.py:127,130`; `loop/reliability.py:113,114`; `profiles.py:184`; `agent.py:880,881,887,1051,1064,1898` |
| `permission_engine` | 13 | `loop/contracts.py:105`; `loop/startup.py:147`; `scheduler.py:1221,1273`; `agent.py:1001,1178,1320,1482`; `profiles.py:186,187,188,205,207` |
| `tool_pipeline` | 5 | `loop/contracts.py:200`; `scheduler.py:563`; `agent.py:748`; `profiles.py:189,194` |
| `_turn_stopping_dispatcher` | 4 | `agent.py:798`; `loop/runtime.py:516`; `tools/search.py:69`; `profiles.py:191` |
| `system_prompt` | 3 | `agent.py:1000,1243`; `profiles.py:217` |
| `_system_prompt_config` | 3 | `agent.py:974,1192`; `profiles.py:216` |
| `_cached_system_blocks` | 2 | `agent.py:950,953` |
| `max_turns` | 2 | `loop/contracts.py:378`; `loop/runner.py:401` |
| `output_schema` | 5 | `loop/contracts.py:31,420`; `loop/request.py:244`; `loop/finalize.py:347,469` |
| `budget` | 3 | `loop/contracts.py:357`; `loop/runtime.py:657`; `ext/workflow/engine.py:87` |

There are **51 `session.agent` occurrences: 49 loads and 2 stores** (the stores
are `profiles.py:195,237`). Direct requested-field loads are `tools` at
`loop/contracts.py:46`, `loop/request.py:262`, and `hooks` at
`loop/runner.py:179,186`. Other consumers use aliases or `getattr`.
The nine explicit aliases originate at `loop/request.py:65,106,194`;
`loop/contracts.py:317`; `loop/runtime.py:469`; `loop/streaming.py:488`;
`hooks/adapters.py:61`; `_telemetry.py:174`; and `profiles.py:174`.
`session.py:424` is the outer event-hook `getattr(self.agent, "hooks")` read.
The dispatcher's own `_hooks` at `hooks/dispatcher.py:83,108` and tools'
`output_schema` fields are not Agent configuration and are excluded.

The prompt path adds **12 indirect system-block/builder sites**:
`agent.py:948,951,954`; `loop/request.py:207,208,216`;
`loop/contracts.py:345,346,353`; `subagents/runner.py:235,242,246`.
These are separate from the requested-field total because they read methods
and derived properties. `_cached_system_blocks_signature` is read at
`agent.py:951`; profile cache invalidation at `profiles.py:227` is write-only.

Profile application/validation reads excluded from Agent totals:
`profiles.py:75,228,229` (`Profile.max_turns`);
`:77,215,218,225` (`Profile.system_prompt`);
`:103,107,231` (`Profile.output_schema`);
`:86,87,100,233` (`Profile.budget`). The independent
`DeepAgentProfile.max_turns` read is `ext/deep_agent/factory.py:172`.
After selection, `RunRuntime.budget` and provider/context budget objects are
accumulators, not additional Agent-budget reads. The two core selection sites
`loop/contracts.py:357` and `loop/runtime.py:657` must agree with the workflow
fallback above.

### Context operations and indirect plugin mutations

The 27-site Context table identifies Agent-root Context accesses. Downstream
registration/resolution operations that must preserve scope ownership are:

| Operations | Count | Exact sites |
|---|---:|---|
| Base Agent effect/service registration | 5 | `agent.py:746,748,750,751,798` |
| Base mailbox resolution / plugin mount / disposal | 3 | `agent.py:807,837,1906` |
| Reliability index resolve/register | 2 | `loop/reliability.py:21,95` |
| Request catalog resolution | 4 | `loop/request.py:67,196`; `loop/startup.py:55`; `tools/search.py:199` |
| RAG service/effect registration and lookup | 5 | `rag/__init__.py:135,158,170,240,248` |
| ToolSearch plugin service/listener/tool effects | 4 | `tools/search.py:67,69,70,102` |
| Mailbox plugin service/inbox/tool effects | 3 | `coordination/mailbox/__init__.py:39,40,44` |
| AskUser tool effect returned to mount | 1 | `tools/ask_user.py:124` |
| Current profile child and local services | 3 | `profiles.py:177,193,194` |
| Lazy telemetry cache resolve/register | 2 | `_telemetry.py:164,167` (Context captured at `:163`) |
| Telemetry tracer/meter/provider resolution | 6 | `_telemetry.py:128,129,131,132,140,143` |
| Kernel inbox Agent resolution | 1 | `kernel/context.py:221` |
| **Total operations above** | **39** | Distinct source-line operation sites. |

The supplied host Context is also read during construction at `agent.py:606`
(`execution` fallback) and `:741` (owned Agent child creation): **2 bootstrap
sites**, separate from accesses to the constructed Agent's Context.

Central kernel machinery is counted separately from those caller sites:

- Plugin mounting: `kernel/plugins.py:59,60` installs the host service-change
  listener and unmount effect; `:103` resolves requirements;
  `:138,140,143,144` creates an activation scope, runs guarded setup, owns its
  disposer and audits services; `:157,164` withdraws/publishes exports
  (**9 sites**). Raw shadow/export checks also occur at `:75,77,82,136`
  (**4 sites**) while classifying changes or detecting dependency cycles.
- Attribute/parent service resolution is `kernel/context.py:128,119`
  (**2 sites**). The shared `get` implementation reads direct services at
  `:109,110` and live exports at `:111,114,115,117` (**6 sites**).
- Inbox target/registration/ownership is `kernel/context.py:221,222,225`
  (**3 sites**); child event-fork/creation/ownership/relay registration is
  `:246,247,248,249,256` (**5 sites**).
- Context's effect/listener registration delegation is
  `kernel/context.py:94,151,152,172` (**4 sites**). Plugin export installation,
  withdrawal and their service-change notifications are at
  `kernel/context.py:284,292,286,294` (**4 sites**); these stay in the kernel
  rather than acquiring run-specific policy.
- Inbox delivery is `loop/runner.py:585`, `loop/runtime.py:245` (**2 sites**).
  Scheduler event dispatch takes the Agent bus at `scheduler.py:1619`
  (**1 site**). Preserve message/event logging and producer iterator closure.

Built-ins currently resolving `ctx.agent` and capturing/mutating configuration:
ToolSearch `tools/search.py:65,69,77,86,97,99,102`;
Mailbox `coordination/mailbox/__init__.py:38,41,44`;
AskUser `tools/ask_user.py:124`. These **11 sites** overlap the tables above.
The mailbox producer additionally reads `session.agent.durability` at
`coordination/mailbox/__init__.py:54`; durability stays Agent-owned.

### Bootstrap, live extensions, and children

`Agent.session()` checks/mounts ToolSearch at `agent.py:1527,1530` and connects
MCP, skills, and subagents at `:1533,1535,1537` (**5 sites**). Skill loading
reads tools at `:1295`, registers Skill at `:1310`, and appends permission rules
at `:1320`. Subagent setup registers at `:1392,1398,1406`; reload unregisters
at `:1425,1426,1427`. MCP attachment registers at `:1476`, extends rules at
`:1482`, refreshes the cache at `:1483`, and is called by the public mid-run
connection path at `:1497`. The input-only subagent parallel resolver reads
the base catalog at `:1381` and checks overrides at `:1371,1373,1374,1375`.

There are **7 `parent_agent` occurrences**: declarations at
`subagents/runner.py:35,73`; aliases at `:205,363`; call arguments at
`tools/subagent.py:234`, `tools/subagent_continue.py:114`, and
`ext/workflow/context.py:253`.

There are **2 direct Session constructors**: `agent.py:1563` (publishes into
the Agent registry at `:1614`) and `subagents/runner.py:255` (child owner at
`:259`, registration at `:282`). There are **7 delegated/example Session
creation sites**: `sessions/fork.py:153`; `ext/workflow/engine.py:66`;
`worker/runtime.py:388`; `ext/loop_runner.py:476`;
`ext/evals/harness.py:158`; `ext/scaffold/_templates.py:144,181`
(the last two are generated examples). `Agent.fork_session` at `agent.py:1699`
delegates to `sessions/fork.py:37`; there are no other source call sites.

Other descendant configuration/lifetime sites are
`subagents/runner.py:224,235,242,246,276,291,347,348,389`
(tools, prompt builders, mailbox address, hooks, release);
`tools/subagent_stop.py:93` (owner's Session registry);
`tools/subagent.py:322` (owner's run store); and the existing copied-child
cleanup comparisons `profiles.py:247,257` (**13 sites**).

## One owner and one configuration accessor

Add a private, slotted `_ProfileRunOverlay` in `profiles.py`. It contains the
real owning Agent, the pinned Profile reference, a child Context, and only the
run configuration surfaces listed below. It has no session registry, session
lock, provider lifecycle, store lifecycle, or `close()` implementation.

Use one private accessor, `_effective_run_config(session, run_id)`, for
Agent-derived run configuration. Its result is a narrow configuration surface,
not an Agent substitute. Resolve it once at the run boundary and capture it in
`RunRuntime` and the existing provider/tool/finalization contexts. Ownership,
admission, provider preparation, session creation/release, and shutdown continue
to use `session.agent` directly. Do not replace the scheduler's Agent argument
with an object that impersonates the Agent; pass its captured configuration
alongside the real owner where necessary.

The internal API is
`_effective_run_config(session: Session, run_id: str) -> _RunConfig`.
`_RunConfig` exposes `context`, `tools`, `hooks`, `permission_engine`,
`tool_pipeline`, `_turn_stopping_dispatcher`, prompt/block-building state,
`max_turns`, `output_schema`, and `budget`. It exposes no Agent lifecycle
operations. Missing/foreign run bindings must not silently select another live
run's overlay; ordinary runs explicitly select their base configuration.

The accessor looks up an explicit run binding. A plain run uses the real
Agent's defaults and current session overrides, without creating a child Context
or forking registries/listeners. A profile run uses its overlay. The internal
configuration interface lists its supported fields; do not add arbitrary
`__getattr__` forwarding to an Agent. Normalize the existing override precedence
at the run boundary rather than reimplementing it in each consumer.
Keep the ordinary run's live Agent registry behavior, including mid-run MCP
attachment. Profile tool views retain their existing composition boundary;
host loading/preparation finishes before composition, and later host-global
mutations do not silently widen an already-admitted profile view.

| Surface | Effective source and ownership |
|---|---|
| Tools | Copy the session override if present, otherwise Agent tools; mount profile plugins into that view, then intersect the profile allow/deny rules and the original session restriction. Plugin additions cannot widen a session restriction. Ordinary runs retain existing registry behavior. |
| Permissions | Preserve the host engine's rules, callbacks, role/principal behavior, and project root; add the existing deny-only profile layer in an overlay-local engine view. An allow list grants no host-denied permission. |
| Hooks | Snapshot the host hook registrations into a run-local list for a profile; hook objects keep their existing host ownership. Profile-added registrations belong to the overlay scope. |
| Tool pipeline | Fork the host pipeline for a profile, preserving listener order and policy identities. Profile listeners never register on the host pipeline. |
| Turn stopping | Snapshot the host dispatcher for a profile; register profile listeners in the run dispatcher. Keep the run-local reliability listener binding and disposal. |
| Prompt and cache | Append the role instructions using the existing SystemPromptConfig semantics. Respect a session system-block override. The overlay owns its prompt configuration and system-block cache/signature, including admitted tool names and contributed sections. Never invalidate or populate the parent cache for profile input. |
| Turn limit | Profile `max_turns` when supplied, otherwise the Agent default. |
| Output schema | Explicit RunOptions schema, then profile schema, then Agent default. |
| Budget | Explicit RunOptions budget, then inherited parent budget, then a fresh profile RunBudget, then Agent default. Children still share the selected parent spending cap. |

Reuse/extract the existing pure system-block building/signature code rather
than call Agent methods with an overlay as `self`. The same builder serves the
Agent and the overlay, so protocol blocks, tool sections, security posture,
prompt caching, and session/fork prefixes keep their current meaning.

`RunContract` composition, checkpointable hook enumeration, startup preparation,
requests and retries, compaction reconstruction, scheduler policy, final tools,
and finalization all consume this captured configuration. Merely changing
`loop/request.py` would leave authorization and resume inconsistent. Keep the
profile reference/hash and plugin execution order in the existing contract;
overlay objects and effect handles are never serialized. Direct resume and
worker takeover reconstruct the same configuration before contract validation.
`Session._iterate` must also capture the effective hooks for its outer
`EVENT_EMIT` dispatcher; changing only the inner loop would omit profile-added
event-emission hooks.

## Contexts, plugins, identity, and delivery

**Resolve run services from the effective configuration's Context.** For a
profile this is the overlay child, with normal fallback to the real Agent
Context and then its host parents. For an ordinary run it is the real Agent
Context. Agent-owned services such as telemetry, session/store ownership, and
the reliability run index remain on the Agent Context; the overlay resolves
them through fallback. A service lookup must not lazily install an Agent-owned
singleton into a disposable profile Context.

**Register profile plugin effects in the profile activation scope.** Create the
overlay child with `isolate_events=True`, then mount its plugins with the
existing `_PluginMount` on that child. Each activation still gets its own
audited child scope, declared `provides`, reversible registrations, service-change
relay, and retryable disposal. Keep host listeners as snapshots and never add
unfiltered profile listeners to the host bus. Service changes may cross the
existing filtered relay; run/tool events dispatch through the captured run bus.

Register the real Agent as `agent` only in its owned Agent Context. Do not
shadow that service in the profile child. Add a `run_config` capability in the
Agent Context and shadow it with the overlay configuration in the profile
child. Thus `ctx.agent` is always the real owner, while `ctx.run_config` exposes
tools, hook registration, pipeline, stopping dispatcher, and prompt/limit
configuration for the mounting scope. The base capability targets the Agent's
configuration; the profile capability targets the overlay. These are explicit
configuration operations, without session or shutdown methods.

Migrate ToolSearch, Mailbox, AskUser, and the profile test plugins to use
`ctx.run_config` when installing tools/hooks/listeners. `ctx.effect` owns their
returned disposers. Host profile plugins that mutate `ctx.agent.tools` or call
Agent-global registration methods must make the same migration; do not hide
that mutation behind a copied/proxy Agent or redirect it with ambient global
state. The Plugin protocol and `setup(ctx, config)` signature stay unchanged.
No new top-level public export is needed.

Strict Agent identity checks stay strict. `session.agent is self.agent` becomes
true for an extension's own profile run and remains false for another Agent,
even when both Agents share a host Context. PrerequisiteHook's existing
`ctx.session.agent.context is not self.context` exclusion stays unchanged:
both sides now identify the same real Agent Context for its own profile run.
Do not replace it with ancestry, shared-parent, or service-equality tests.

Stable Agent identity alone does not select a profile-local service. For
example, ToolSearch's terminal guard must compare the effective run Context's
`request_catalog` with its catalog instance, rather than always resolve from
`self.agent.context`. Apply the same ownership-plus-effective-service rule to
profile-local RAG and catalog bindings so a shadowed host registration cannot
act twice. Run data remains keyed by session/run, never by the Agent alone.

### Inbox and mailbox ownership

Keep the host inbox bucket on the real Agent Context. Give each overlay its
own bucket. `Context.inbox()` targets the Context of the resolved `run_config`
capability (falling back to the resolved Agent for standalone/existing Context
use), while the registering activation scope owns the disposer. This is
necessary because plugin activation scopes are children of their mounting
Context; storing producers only on the activation's private bucket would make
them invisible to the current boundary drain.

At a run boundary, snapshot and drain the Agent bucket in its registration
order, then the overlay bucket in its registration order. Do not traverse every
descendant Context and do not copy Agent producers into the child bucket.
Each registration is invoked once. Preserve the existing no-producer fast path,
message logging, durable delivery/ack semantics, and iterator `aclose()`.

The globally mounted mailbox service and its producer remain Agent-owned, so
an empty Profile still delivers the host mailbox. A profile mailbox service
and its producer remain overlay-owned. Producers are additive registrations:
mounting a profile mailbox does not implicitly remove a host producer. Replacing
a delivery source requires an explicit registration/disposer decision, not
inference from service-object equality. Mailbox lookups during a run (including
child address setup) use the effective Context; `Agent.mailbox` continues to mean
the host service. These rules preserve multi-Agent and concurrent-run isolation.

## Run binding, descendants, and disposal

Store the active overlay binding on the Session for foreground admission and
keep retained bindings indexed by run id until released. Reserve the launch
run identity before profile setup; explicit/queued run ids and resume ids keep
their current identity. Publish `active_run_id` at the existing execution
boundary. The accessor requires that explicit run identity. Hook contexts
already carry it; tool/background contexts must capture it rather than fall
back to whatever `session.active_run_id` says later.
Install the provisional binding before activating plugins so their callbacks
can resolve it, but admit no provider/tool execution until setup succeeds.
Apply the same ordering to queued launches, resume, and child runs.

A Session attribute is only an index/ownership root. It is not the authority
for detached work's configuration. Capture the binding/configuration in
RunRuntime, scheduler closures, background tasks, inbox dispatch, and subagent
arguments before detaching work. In particular, run A's background task must
retain A's configuration after the Session finishes A and starts run B. Never
resolve a detached task through a mutable “current profile” attribute or an
unkeyed ContextVar.

The binding owns one foreground retain. Acquire a retain **before** scheduling
each detached background/worker task, and release it in that task's finalizer.
Child creation acquires a retain before its first await; a failed creation
releases it. Every child Session's `agent` and every `parent_agent` argument
remain the real Agent. Pass the captured configuration separately to the
subagent runner, derive the child's tools/system blocks with the existing
definition/fork restrictions, and retain the profile services for the child's
run. A child run gets its own run-id binding; its inherited configuration seed
is not a replacement Agent.
If the child explicitly selects another Profile, compose it on that captured
seed and use the inherited profile Context as the new child scope's parent.
Child tool restrictions still intersect the inherited boundary, and inherited
budget precedence still applies. This preserves originating profile services
without making an unrelated Session's profile visible to the child.

A retained child Session keeps a configuration/service retain between runs so
SubagentContinue still sees the originating profile services. Continuing it
derives a new child-run binding from that seed, not from a later profile on the
parent Session. A nonretained child releases its Session retain on removal;
a retained child releases it only after successful `Session.aclose()` /
`Agent.release_session()`. Track child ownership by explicit binding identity,
replacing `_profile_scope.release()`'s `child.agent is copied_agent` scan.
Ordinary `Agent.fork_session()` remains an independent conversation; only the
explicit subagent fork path inherits the live profile binding.
The input-only subagent parallel resolver must continue to fail closed when
it cannot identify the effective restricted catalog. Removing temporary
`Session.tools_override` writes must not make that resolver overlook a profile.
This changes configuration lookup, not the read-only parallelism rule.

Foreground exhaustion, error, abort, cancellation, early iterator close, and
profile setup failure release the foreground retain in `finally`. Restore only
Session staging fields that were changed; do not assign `session.agent` at any
point. Prefer storing effective tools/system blocks on the binding so profile
entry need not temporarily overwrite Session overrides. Failed plugin setup
must complete/retain rollback before publishing a runnable binding.

After the last retain, dispose the child Context through one coalesced cleanup
attempt. Await cleanup when the closing run/child is the final owner; task
completion callbacks observe asynchronous cleanup failures. On teardown failure,
keep retry ownership in the parent Context and retain the unfinished binding
until cleanup succeeds or Agent shutdown retries it. Do not remove the only
handle to a failed disposer. Successful cleanup removes the Session run-id
entry and the parent effect entry (82-2), so memory is bounded by live work.

Session/Agent close first reject new work, abort/close the active iterator, and
drain tasks and retained descendants as today, then release configuration
bindings before disposing shared services. Agent lifecycle state, idle pruning,
the registry, and the lock have one owner. This removes the copied stale-open
state and copied-Agent `release_session()` mismatch in 87-2; do not weaken the
existing “session belongs to a different agent” check or add another lock.

## Finding coverage and phase 2 regression plan

Each case below starts with its failing reproduction against phase 1 code,
then receives the smallest overlay change that makes it pass. Use
`tests/support` fake providers/tools and in-memory stores. Storage behavior is
unchanged; any discovered backend parity case belongs in the shared two-backend
conformance suite.

| Finding | Design change | Required regression behavior |
|---|---|---|
| 87-1, RAG | Stable Agent identity; captured effective Context and hook/dispatcher views | With `register_rag_connector(agent, conn)`, compare plain and empty-Profile runs: retrieval occurs and `[doc:d1]` reaches the request. Verify grounding still rejects an unsupported answer. Keep another Agent using the same host Context isolated. Put cases in `tests/rag/test_rag.py`. |
| 87-1, mailbox/inbox | Drain the real Agent bucket plus the run-local bucket | With `agent.use(MailboxPlugin(), box)`, an empty-Profile run delivers `peer hello`, logs it once, and consumes/acks the right queue entry. Add a profile-local producer case that neither delivers to a concurrent plain run nor survives disposal. Put delivery cases in `tests/loop/test_inbox.py` and registration cases in `tests/kernel/test_context.py`. |
| 87-2(a) | Real Agent for children, shared lifecycle ownership; explicit config retain | With `max_idle_sessions=10`, start a profile Subagent, begin `agent.close()` while the child runs, and release the child barrier. Close completes within a bounded test deadline; child and parent finish closing without a registry-lock waiter. Put pruning coverage in `tests/agent/test_idle_sessions.py` and close coverage in `tests/session/test_session_lifecycle.py`. |
| 87-2(b) | Same owner during idle pruning and retained-child release | With `max_idle_sessions=1` and `retain_subagents=True`, run a profiled retained child. Assert no “different agent” ConfigError, no child stuck in quiescing, and idle-tree eviction/explicit release succeeds. Preserve rejection of an actual foreign-Agent Session. |
| 73-6 | Stable real Agent Context; effective hooks include host prerequisite hook | Configure `ToolPrerequisite("deploy", ("run_tests",))`; call deploy first under an empty Profile and verify the exact prerequisite error and zero deploy side effects. Then run tests successfully and verify deploy is admitted. Put this in the existing reliability/prerequisite test module. |
| 78-1 | Stable Agent identity, effective request catalog and event bus | With 31 tools and an empty Profile, call deferred `lookup_2` before search. The initial offer remains only `tool_search`; the resolve guard returns the deferred error before validation, hooks, permissions, or execution. After `select:lookup_2`, it runs. Cover the terminal guard and a profile-local catalog shadow separately in `tests/tools/test_search.py`. |

Add focused lifecycle/behavior cases in `tests/profiles/test_profiles.py`:

- Concurrent profiles and an ordinary run share one Agent; tools, hooks,
  permissions, pipeline, stopping listeners, prompt caches and plugin effects
  remain isolated. The real `session.agent` is observable inside hooks/tools.
- Run A ends with a detached task, run B starts on the same Session, then A's
  task executes a config/service read: it still sees A and releases only A.
- Foreground/background retained children keep their service after the parent
  run ends; continuation uses that service, and final child release awaits
  cleanup. Replace assertions on `child.agent.context` profile services with
  assertions through the child's captured effective configuration. An explicitly
  profiled child retains the parent service fallback and cannot widen its
  inherited tool restriction.
- Setup failure, early generator close, cancellation, failed cleanup/retry, and
  Agent shutdown release each retain exactly once and leave no disposed scopes
  in the Agent effect list. No-profile request/contract characterization remains
  byte-identical; budget/schema override and resume/takeover tests keep passing.

## Estimated phase 2 diff and review boundary

Estimate **450–700 source lines changed/added** and **250–400 test lines**, across
`profiles.py`, `session.py`, `agent.py` prompt/config registration helpers,
`kernel/context.py` inbox targeting, existing loop context/request/contract/
startup/runtime modules, scheduler configuration capture, RAG/ToolSearch,
Mailbox/AskUser, and subagent creation/continuation. This is an estimate, not a
line budget; inventory replacements and shared prompt extraction dominate.
Changes outside WP C's present source ownership require coordination before
phase 2 begins, particularly scheduler and plugin/tool call sites owned by D.

No new runtime engine, scheduler parallelism rule, storage schema, public
Agent facade, or mutable process-wide registry is proposed. Phase 1 stops at
this committed design. Findings 87-1, 87-2, 73-6, and 78-1 remain deliberately
unfixed until the architecture owner reviews the overlay and lifecycle design.

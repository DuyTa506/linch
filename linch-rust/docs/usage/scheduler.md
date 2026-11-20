# Scheduler

Use this page to run an agent in the background: on a cron pattern or an
interval, as a heartbeat that keeps checking in on one session, or once,
queued for a worker.

[<- Usage guide](./README.md)

[Tiếng Việt](../vi/usage/scheduler.md)

A schedule and a queued run are rows in the agent's Postgres database. They
do nothing until a **worker** runs: a `linch-server --config FILE` whose
config has a `worker` section on the same database and schema. The client
connection that created them never runs them, and a stdio server (`Server()`)
runs no worker.

```text
client ── schedule.create / job.enqueue ──> Postgres (schedules, jobs)
                                                │
linch-server --config (worker section) ─────────┘  fires schedules, takes jobs,
                                                    opens the stored agent, runs it
```

## Run a worker

Add a `worker` section to the [server config](./server.md#config). A
server may run only a worker (no `socket` or `http`); its tenants then need
no token.

```json
{
  "socket": "/run/linch/linch.sock",
  "worker": {"dsn": {"env": "LINCH_WORKER_DSN"}},
  "tenants": {
    "acme": {
      "tokens": [{"env": "LINCH_TOKEN_ACME"}],
      "allow": {
        "postgres_dsns": ["postgres://linch_acme@db.internal:5432/linch"],
        "max_schedules": 20,
        "max_queued_jobs": 200,
        "min_schedule_interval_ms": 60000
      }
    }
  }
}
```

| `worker` field | When absent | Meaning |
|---|---|---|
| `dsn` | required | `{"env": "VAR"}` or `{"value": "..."}`: the database and schema whose queue it serves. Background runs store through it |
| `owner` | the server's `server_id` | The name its job holds and run claims carry; unique per worker |
| `queues` | `["default"]` | The queues it takes jobs from, one from each in turn |
| `max_jobs` | 32 | Jobs it runs at once |
| `max_connections` | the default pool size | Its own pool for the queue; each run uses its tenant's pool |
| `poll_interval_ms` | 1000 | Longest wait between polls; due schedules are checked once per interval |
| `visibility_timeout_ms` | 30000 | How long a taken job stays hidden without a heartbeat. Keep the agents' `stores.lease_ttl_ms` no longer |
| `retry_delay_ms` | 5000 | Wait before a failed job's next attempt; doubles each time, at most 5 minutes |
| `history_retention_ms` | 7 days | How long ended and failed jobs stay readable with `job` and `jobs`; a job of a run that is not terminal stays longer ([what is pruned](#what-is-pruned)) |
| `drain_timeout_ms` | 30000 | How long the worker waits for its jobs in flight after SIGTERM or SIGINT before it hands them back ([Stopping a worker](#stopping-a-worker)); at least 1, a 0 is refused when the config loads |
| `retention` | absent: nothing is deleted | Delete what ended runs and idle sessions leave behind, in small batches: `{"terminal_runs_days": 30, "idle_sessions_days": 90}`, with optional `tombstone_days` (30), `batch_size` (500) and `interval_secs` (3600). One pass runs at a time across all workers on the database. [Stores: Retention](./stores.md#retention) says what goes and what stays |
| `local_namespaces` | none | Namespace roots outside every tenant whose jobs it also runs, without checks (schedules a stdio client made); `dev` covers `dev` and `dev/...` |

Run as many such servers on one database as you like: each fire is queued
once and each job runs on one worker at a time. A server without a `worker`
section runs no loop and sends no query for it.

### Stopping a worker

On SIGTERM or SIGINT the server stops taking jobs and waits up to
`drain_timeout_ms` (30 s by default) for the jobs in flight to finish. A job
still running then is handed back: its run stops where it stands, the run's
lease is released (the server waits up to 5 s for that), and the job is
visible again at once with no failure counted, so another worker resumes the
run from its last durable state. The server then exits. A stuck job cannot
hold the process: it ends after the drain timeout, the releases and, at the
very most, 10 more seconds.

Give your supervisor a termination grace period longer than
`drain_timeout_ms` plus about 15 s. Kubernetes' default is 30 s, the same as
the default drain timeout, so lower `drain_timeout_ms` (to 10000, say) or
raise `terminationGracePeriodSeconds`. A worker killed with SIGKILL hands
nothing back: its jobs wait out `visibility_timeout_ms` and its runs the
lease TTL.

A schedule or queued run is stored in the database and schema of the
agent that made it, and a worker serves only those of its own `dsn`, where
it also runs them. An agent whose store DSN names another database (or
schema) needs a worker there: no other worker ever fires its schedules.
Point `worker.dsn` at the same database and schema as the agents' store
DSN. When they differ only in the user, the background runs connect as the
worker's user.

The worker runs the jobs of every tenant in the config. Each job's agent is
checked against its tenant's allowlists again at every attempt, as
`open_agent` would be, except the store DSN, which is the worker's. So a
tenant whose allowlist was tightened stops running at the next fire: that
job fails with the reason. The same holds for a stored definition made before
a tenant had to bound its runs (see `run_bound` in
[Server](./server.md#what-admission-refuses-and-why)): one with neither
`max_turns` nor a budget cap fails its queued job, and each fire of its
schedule, with a reason that names both settings, which `job.list` shows; the
worker goes on with the other jobs. Set `max_turns` or a budget on the agent
and open it again to schedule it anew. Each background run takes one of the tenant's
`max_runs` places while it runs; with none free the job waits.

## What a tenant may schedule

| `allow` field | When absent | Effect |
|---|---|---|
| `max_schedules` | no schedules | Schedules the tenant may keep in each database, across its namespaces. Absent: `create_schedule` raises `NotAllowed` (`kind == "schedules"`); at the cap, `LimitReached` (`kind == "schedules"`) |
| `min_schedule_interval_ms` | 60000 | The shortest time between two fires: an interval, or the closest fires of a cron pattern (over its next 512 fires). Faster: `NotAllowed` (`kind == "schedule_interval"`) |
| `max_queued_jobs` | no background runs | Unfinished jobs (queued, running or retrying) the tenant may have when it calls `enqueue`. Absent: `NotAllowed` (`kind == "background_runs"`); at the cap, `LimitReached` (`kind == "jobs"`). A schedule's fire at the cap is skipped (counted in `skips`), never failed; its jobs count toward the cap. Absent, fires have no cap |

A stdio client has no tenant and no caps.

A tenant with neither `max_schedules` nor `max_queued_jobs` never migrates the
database, but its reads stay available so it can clean up what it made:
`schedule.list`, `schedule.delete`, `job.list` and `job.get` read the worker's tables where
they exist, at whatever migration they are at, and answer as an empty namespace
only where the tables do not exist. If a read fails because the tables are older
than the server's, the answer is a generic `StoreUnavailable` (`-32014`) that
names no table, column or migration.

## Which agents can run in the background

The agent you schedule needs Postgres stores, no host tools or callbacks,
and no `remote` execution backend for `Bash` (its commands run on the
client's machine): a background run has no client to call back. Otherwise the
call raises `NotBackground` with `reason` `memory_store`, `host_tools`,
`callbacks` or `remote_execution`. Give the agent workspace or MCP tools, and
a `local`, `sandbox` or `docker` backend, instead.

A schedule always runs the config it was created with; open the agent again
and create a new schedule to change it.

### What is stored at rest

The agent's config, as it was opened (after the tenant's checks), is stored
in plain text in the `linch_worker_agents` table of the agent's database,
once per distinct config, because the worker must open the same agent
again. Everything in it is stored **except the store DSN** (the worker
connects with its own):

- MCP stdio `env` values and arguments, and MCP HTTP URLs and header
  values, as given;
- the provider settings, including any request headers and extra body
  fields the config sets;
- the rest of the config: `spec`, permissions, workspace, profiles.

Job, history and schedule rows name the config by its SHA-256 digest, and
no `schedule.*` or `job.*` answer or error carries its values, nor does the
error of a background run the worker could not open. A run's own error
is another matter: it holds what the provider, a tool or an MCP server
said, and an endpoint that quotes back a credential it was sent puts it
there, as in a run started directly. A stored config is deleted once
no schedule, no unfinished job and no run that is not terminal names it: a worker checks from its first
poll and every 10 minutes after (`prune_definitions`). Keep secrets out of
a scheduled config where you can (a key the server reads from its
environment is never stored), and protect the database as you protect
those secrets.

## What is pruned

A worker prunes every 10 minutes: the history of ended jobs and the failed
jobs older than `history_retention_ms`, and the stored configs nothing names.
A history row or failed job that names a stored config stays while its run
is not terminal (suspended for an approval or an answer, or its job failed),
however old: it is how `approve` and `resume` find the agent to continue the
run with. The next prune after the run ends removes the row and then the
config.

**Runs and sessions are not pruned.** Only jobs and their history are. Each
fire leaves a run (its events and checkpoint) and, for a schedule with no
`session_id`, a session: about 1,440 of each a day for a schedule that fires
every 60 seconds, for as long as the database keeps them. No server call and
no setting removes them, and their rows carry no timestamp. An operator who
must remove them does it with SQL on the agent's database, over
`linch_runs` and `linch_run_events` (terminal runs only) and
`linch_sessions` and `linch_session_entries`, and only for runs no job
still names; prefer a longer interval, or a heartbeat (one `session_id`),
which keeps the sessions to one.

## Schedule a run

```python
from linch_rust import Server

async with Server.connect("unix:/run/linch/linch.sock", token=token) as server:
    agent = await server.open_agent(config)  # Postgres stores, no host tools

    # Every day at 07:00 Berlin time, each fire in a new session.
    digest = await agent.create_schedule(
        "Write the daily digest.", cron="0 7 * * *", timezone="Europe/Berlin", id="digest"
    )
    print(digest["next_fire"])  # RFC 3339, UTC

    # Every 15 minutes.
    await agent.create_schedule("Check the queue.", interval_ms=15 * 60 * 1000)
```

| Call | Does |
|---|---|
| `await agent.create_schedule(prompt, *, cron=None, timezone=None, interval_ms=None, session_id=None, id=None, queue=None, principal=None)` | Store a schedule; give `cron` (five fields, or six with leading seconds) or `interval_ms`. `timezone` is an IANA name, UTC when absent. `id` defaults to a uuid |
| `await agent.heartbeat(session_id, *, interval_ms=None, cron=None, timezone=None, prompt=CHECK_IN, id=None, queue=None, principal=None)` | A schedule whose fires continue one session (below) |
| `await agent.schedules()` | The schedules of the agent's namespace, by id |
| `await agent.pause_schedule(id)` | Stop its fires; queued runs stay queued |
| `await agent.resume_schedule(id)` | Fire again from the next tick after now |
| `await agent.delete_schedule(id)` | Delete it; `False` when there was none. Runs already queued or running finish |

A `Schedule` is a dict: `id`, `namespace`, `queue`, `prompt`,
`session_id` (fixed sessions only), `cron` and `timezone` or
`interval_ms`, `paused`, `next_fire`, `last_fire`, `fires` (fires that
queued a run), `skips` (fires not made: skipped while busy or at a cap, or
missed while every worker was down) and `created_at`.

### Time zones

A cron pattern is read in its time zone, daylight saving included. A fire
time that does not exist (02:30 on the night clocks go forward) moves to the
first valid time after the gap; one that happens twice (01:30 on the night
they go back) fires once, the first time. An interval ignores time zones.

### Sessions

Without `session_id`, each fire starts its own session,
`schedule:<id>:<fire time>`, so fires never share a conversation. A fire
is still skipped (counted in `skips`) while the schedule's previous fire's
job is unfinished (queued, running or retrying), so a backlog never stacks
a job per fire; a fire whose run suspended for a person no longer blocks
the next.

With `session_id`, every fire is a new run in that session: a heartbeat.

### Who a background run acts for

Pass the same `principal` you would pass to `agent.run`
([permission rules](./tools.md#permission-rules)). It is stored with the schedule or queued run,
and every fire's run (and its resume or approval by the worker) acts for
it, so a permission rule with `role` or `unless_role` holds in the
background as it does for a run started directly. Without one, the run
holds no role: a rule with `role` never matches it and one with
`unless_role` always does. A tenant's principal must name the tenant
(`NotAllowed`, `kind == "principal"`). Schedule for a restricted caller
with that caller's principal, never without one.

## Heartbeat: one session, checked in on

```python
beat = await agent.heartbeat("ops-room", interval_ms=30 * 60 * 1000)
```

Each fire is a new run in `ops-room` prompted with `CHECK_IN` (or your
`prompt`), so the agent sees the whole conversation so far. Two fires
never overlap: **a fire is skipped** (counted in `skips`, not queued) while

- the previous fire's job is not finished (queued, running or retrying), or
- the session has a run that is not terminal: one in progress (yours from a
  client, too), or one suspended for an approval or an `AskUser` answer.

Skipping, not queueing behind, is deliberate: a check-in that waited behind
a long run would only repeat the next one, and a run that waits for a person
would otherwise stack a job per fire. To continue a suspended heartbeat run,
read its `run_id` from `agent.jobs(schedule_id=...)` and answer it with
`agent.approve` or `agent.answer` on an agent of the same config, or end it
with `agent.cancel(run_id)`; the next fire after it ends runs again.

An unattended run that needs approval suspends like any other: the job is
`done`, the run waits. Use a permission mode or rules that allow what a
background run must do.

## Queue a one-off run

```python
job = await agent.enqueue("inbox", "Summarize the inbox.")
done = await agent.wait_job(job["job_id"], timeout=300)
if done["state"] == "done":
    events = await agent.events(done["run_id"])
```

| Call | Does |
|---|---|
| `await agent.enqueue(session_id, prompt, *, queue=None, principal=None)` | Queue a run; a worker starts it. A session with a live run makes it wait (retrying) until that run ends |
| `await agent.job(job_id)` | One job of the agent's namespace, live or ended |
| `await agent.jobs(*, schedule_id=None, limit=None)` | Jobs newest first (50 by default, at most 500); only one schedule's with `schedule_id` |
| `await agent.wait_job(job_id, *, poll=0.25, timeout=None)` | Poll until the job is `done`, `failed` or `superseded` |

A `Job` is a dict: `job_id`, `queue`, `state`, `session_id`, `run_id`
(once a worker started it), `schedule_id` and `fire_at` (for a fire),
`attempts`, `failures`, `error`, `created_at`, `finished_at`.

`failures` counts the attempts that failed, the last one of a job that
`failed` included (a job that gave up after 5 attempts reads `failures: 5`).
A busy session is a retry, not a failure. It is kept with the live job only:
a `done` or `superseded` job reads `failures: 0`.

| `state` | Means |
|---|---|
| `queued` | Waiting for a worker |
| `running` | A worker holds it and drives its run |
| `retrying` | An attempt ended without finishing it; `error` says why (a failure, or a busy session). It is tried again later |
| `failed` | It can never succeed, or failed 5 times; `error` says why. Its run, if any, is failed too when it cannot continue |
| `done` | Its run ended, or suspended for an approval or an answer |
| `superseded` | Another owner claimed its run, which goes on there |

The run's events are durable: read them with `agent.events(run_id)` on an
agent of the namespace. Background runs send no live stream to any client.

## Let the model schedule

`schedule_tools: True` in the agent config gives the model three tools, so
it can schedule its own follow-ups ("check the deploy every hour"):

| Tool | Scope | Input | Does |
|---|---|---|---|
| `CreateSchedule` | `exec` | `payload`, and `cron` (with an optional `timezone`) or `interval_s` | A heartbeat of this agent in the calling run's session; answers `Scheduled <id> at <next fire>` |
| `ListSchedules` | `read` | none | This agent's schedules in the calling run's session, one line each: `<id>: <payload> enabled=<bool> next_fire=<time>` |
| `CancelSchedule` | `write` | `id` | Deletes one of this agent's schedules in the calling run's session; runs it already queued finish |

```python
config = {
    "namespace": "acme",
    "spec": {"model": "deepseek-chat", "max_turns": 20},
    "provider": {"id": "deepseek"},
    "stores": {"type": "postgres", "dsn": dsn},
    "schedule_tools": True,
}
agent = await server.open_agent(config)
run = await agent.run("ops", "Check the deploy every hour.", principal=ada)
```

The model chooses only the prompt and the timing. The server fixes the
rest, so a schedule never gives the model more than the run that made it
had:

- **The same agent.** Every fire runs the config this agent was opened
  with, stored as `create_schedule` stores it. No config, id, queue or
  session comes from the tool input.
- **The same session and principal.** Every fire is a new run in the
  calling run's session (a [heartbeat](#heartbeat-one-session-checked-in-on),
  so fires skip while the session is busy) and acts for the calling run's
  principal: permission rules on `role` hold for the fires as for the run.
  A call that asked runs after `approve` for the run's principal: an
  approve that names none acts for the principal the run was started
  with, and one that names another acts for that one.
- **Once per call.** The schedule id is the call's `<run_id>:<tool_use_id>`,
  so a retry of the same call finds its schedule instead of making another.
- **The same caps.** A call is admitted as `create_schedule` is: the
  tenant's `max_schedules` (counting the schedules the client made) and
  `min_schedule_interval_ms`. A refusal is the tool's error, with code
  `not_allowed`, `limit_reached` or `invalid_schedule`; the run goes on.
  A failure of the store is the error `the schedule store failed; try
  again later` (code `store`): its cause, which can name the database
  host, goes to the server's `tracing` log, never to the model.
- **Its own session's schedules only.** `ListSchedules` and
  `CancelSchedule` see the schedules of the agent's namespace that run this
  same config in the calling run's session, as Python's tools see one
  session's: not another session's, not a schedule without a session (one
  the client made with `create_schedule` and no `session_id`), not another
  agent's, nor another namespace's.
- **Not from a subagent.** A subagent, in the foreground or the
  background, may hold fewer tools than the agent a schedule runs, so its
  `CreateSchedule` gets an error with code `not_allowed`.
- **Asked like any exec tool.** `CreateSchedule` asks in modes `default` and
  `accept_edits`. A fire that calls it with nobody to approve suspends;
  give the background runs a mode or rules that allow what they must do.

The fires queue on the `default` queue. A fire's run has the tools too,
so it can schedule again, within the same caps.

**Known gap: a changed config orphans its schedules.** The tools find
schedules by the digest of the stored config. Open the agent again with
any change to its config, and `ListSchedules` and `CancelSchedule` no
longer see the schedules the old config's runs made. Those schedules
still fire, running the old config, and count toward `max_schedules`.
List them with `agent.schedules()` and remove them with
`agent.delete_schedule(id)` (`schedule.delete`).

`open_agent` refuses `schedule_tools`:

| Refusal | When |
|---|---|
| `NotAllowed`, `kind == "schedule_tools"` | The tenant's allowlists have no `max_schedules` (deny by default), or the connection is local: a stdio server runs no worker, and a local client has no caps |
| `NotBackground`, `.reason` | Memory stores, host tools, callbacks or a remote execution backend: the fires could not run the same agent |

A worker must serve the agent's database ([Run a worker](#run-a-worker));
the server cannot see a worker in another process, so `max_schedules` is
the operator's word that one runs. A worker also checks it again at every
fire: a tenant whose `max_schedules` was removed fails the fire's job.

## Trace a run to its schedule

A run a schedule fire starts has a `schedule_fire` event (`ScheduleFireEvent`
in the client) right after its `init`, with the `schedule_id` and
`fire_at_ms`, the time the fire was due in Unix milliseconds. It is durable,
so `Agent.events(run_id)` finds it, and it arrives on the run's stream when a
client reads one. A run queued with `job.enqueue` has none, and a resume does
not repeat it. Its envelope is `ignorable`, so an older build reading the run's
events skips it. The OpenTelemetry spans of the run carry `linch.schedule.id`
([Observability](./observability.md)).

## Durability

- **Each fire is queued exactly once.** A due schedule's row is locked, its
  job inserted and its next fire moved in one transaction, so a worker that
  crashes before the commit leaves nothing and the fire is made by the next
  poll; workers that poll at once skip the locked row. Each job also has the
  unique key `schedule:<id>:<fire time>`.
- **Each job runs on one worker at a time.** A taken job is hidden for the
  visibility timeout, which its worker's heartbeat extends. A crashed
  worker's job becomes visible again and another worker takes it; the run's
  lease and epoch fence the old holder out, and the run resumes from its
  last durable state ([Runs](./runs.md)).
- **Missed fires coalesce into one.** After every worker was down, a
  schedule fires once, at the first tick it missed, counts the other missed
  ticks in `skips`, and its next fire is the first tick after now. Python
  Linch fires every missed tick instead; here a day of downtime under a
  one-minute schedule would queue some 1,440 runs, each paid for, so this
  differs on purpose. Fires missed while paused are not made.
- **Deleting a schedule stops only future fires.** Its queued and running
  jobs finish.

## Errors

| Raised | When |
|---|---|
| `NotBackground` (-32040) | The agent has memory stores, host tools, callbacks or a `remote` execution backend; `.reason` |
| `ScheduleNotFound` (-32041) | `pause_schedule` or `resume_schedule` of an id the namespace lacks |
| `ScheduleExists` (-32042) | `create_schedule` with an id the namespace has |
| `JobNotFound` (-32043) | `job` of an id the namespace lacks, or whose history was pruned |
| `InvalidSchedule` (-32044) | A cron pattern that does not parse or never fires, an unknown time zone, an interval out of range, or both or neither of `cron` and `interval_ms`; `.reason` |
| `NotAllowed` (-32004) | A tenant without `max_schedules` or `max_queued_jobs`, or a schedule faster than `min_schedule_interval_ms`, or a principal of another tenant; `.kind`. From `open_agent`: `schedule_tools` without `max_schedules`, or on a local connection (`kind == "schedule_tools"`) |
| `LimitReached` (-32005) | The tenant holds its `max_schedules` (`kind == "schedules"`) or `max_queued_jobs` (`kind == "jobs"`) |

All are `SchedulerError`s except the last two.

## How it differs from Python Linch

| Python Linch (`linch.worker`) | linch-rust |
|---|---|
| `create_schedule(pool, session_id, payload, cron=..., interval_s=...)`; cron in UTC, five fields | `agent.create_schedule(prompt, cron=..., timezone=..., interval_ms=...)`: any IANA zone, five or six fields |
| A schedule always targets one session | Without `session_id`, each fire starts its own session |
| Fires of one session queue behind each other | A heartbeat skips a fire while its session is busy |
| `cancel_schedule` disables future fires | `pause_schedule`, `resume_schedule` and `delete_schedule` |
| The host passes an `agent_factory` to its `Worker` | The agent's config is stored with the schedule; the server's worker opens it |
| `RunRequest.principal` is restored by the worker | `principal=` on `create_schedule` and `enqueue`, given to every fire's run |
| `register_schedule_tools` lets the model schedule; the host binds a session | `schedule_tools: True`: the server binds the agent, its session is the calling run's ([below](#let-the-model-schedule)) |
| Missed fires catch up in order, one run each | Missed fires coalesce into one run; the rest count in `skips` (no runaway cost after downtime) |

## Related pages

- [Server](./server.md): the config, tenants and allowlists.
- [Runs](./runs.md): resume, approve, answer, and what is durable.
- [Stores](./stores.md): Postgres DSNs, schemas and leases.
- [Worker architecture](../architecture/worker.md): the queue, leases and
  schedules inside.

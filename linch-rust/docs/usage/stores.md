# Stores

Use this page to choose where an agent keeps its runs and sessions: memory
for tests and development, Postgres for anything that must survive a restart.

[<- Usage guide](./README.md)

The `stores` field of an [agent config](./agent.md) picks the backend. The
server owns the stores; your process never connects to them.

```python
{"type": "memory"}                                            # the default
{"type": "postgres", "dsn": "postgres://user:pass@db:5432/linch"}
{"type": "postgres", "dsn": "postgres://user:pass@db:5432/linch", "max_connections": 32}
```

---

## Memory or Postgres

| | Memory | Postgres |
|---|---|---|
| Runs and sessions live | In the agent handle, inside the server process | In the database |
| After the server stops | Gone | Kept; a new server continues them |
| Who can resume or approve a run | The `Agent` that started it | Any agent with the same config and tools (the same [contract](./agent.md#the-run-contract)) on any server |
| One live run per session | Per agent handle | Across every server on the database, per namespace |
| One driver per run | No: each process has its own runs | Yes: a run another server drives is refused with `RunLeased` ([Run leases](#run-leases)) |
| Virtual filesystem files | Per session, dropped by `close_session` | Shared by every session of the namespace |
| Session tasks | In memory | In the database |
| Use it for | Tests, notebooks, development | Services, long or approval-gated runs, more than one process |

Both follow the same rules and pass the same conformance tests, so code that
works on one works on the other. Memory stores are not a cache in front of
Postgres; pick one per agent.

## Postgres

```python
import asyncio
import os

from linch_rust import AgentConfig, ResultEvent, RunNotFound, Server
from linch_rust.scripted import MODEL, text_turn

DSN = os.environ["DATABASE_URL"]


def config(namespace: str, reply: str = "unused") -> AgentConfig:
    return {
        "namespace": namespace,
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": [text_turn(reply)]},
        "stores": {"type": "postgres", "dsn": DSN},
    }


async def main() -> None:
    async with Server() as server:
        # Two agents on one DSN share one pool; the first open runs the migrations.
        support = await server.open_agent(config("acme/support", "Ticket noted."))
        billing = await server.open_agent(config("acme/billing"))
        run = await support.run("ticket-9", "My invoice is wrong", run_id="ticket-9-1")
        assert isinstance(await run.result(), ResultEvent)
        try:
            await billing.events("ticket-9-1")
        except RunNotFound:
            print("billing cannot see support's runs")

    # Another server process reads the same run back.
    async with Server() as server:
        support = await server.open_agent(config("acme/support"))
        for envelope in await support.events("ticket-9-1"):
            print(envelope.seq, envelope.event.type)


asyncio.run(main())
```

```text
billing cannot see support's runs
1 init
2 user
3 context_build
6 assistant
7 usage
8 result
```

### The DSN

`dsn` is a libpq-style URL: `postgres://user:password@host:port/database`,
with query parameters such as `sslmode=require` or `options=...`. The server
connects; give the server's host the network path and credentials.

- **Schema.** Tables are created unqualified, so the connection's
  `search_path` picks the schema. To keep linch-rust in its own schema, create
  the schema and put it in the DSN:
  `postgres://user:pass@db/app?options=-c%20search_path%3Dlinch`.
- **Secrets.** No error message, log line or echoed config holds the DSN or
  its password. The DSN is not part of the run contract, so you can rotate a
  password and keep resuming runs.
- **Namespaces.** Every row is keyed by the agent's `namespace`. Agents with
  different namespaces on one database never see each other's runs or
  sessions (`RunNotFound`, as above); agents with the same namespace share
  them. Use one namespace per tenant or product area, such as `acme/support`.

### Tables and migrations

The first agent that opens on a DSN connects and applies the migrations. This
is safe on every start: applied migrations are skipped, and concurrent opens
wait on a database lock. A schema that is already fully migrated is only read,
in one query, and takes no lock: an agent that opens on it does not wait for
another open, or for a migration still running. Applied versions are recorded in
`linch_migrations`, so your own migration tool's table in the same schema does
not collide.

| Table | Holds |
|---|---|
| `linch_runs`, `linch_run_events` | Each run's status and checkpoint, and its durable events (what `agent.events` returns) |
| `linch_sessions`, `linch_session_entries` | Each session's conversation log |
| `linch_files` | The virtual filesystem, per namespace |
| `linch_tasks`, `linch_task_edges` | Session tasks and their dependencies |

Migration 6 adds two nullable columns to `linch_runs` (`lease_holder`,
`lease_until`) for [run leases](#run-leases). It rewrites no rows and takes
no long lock. A server of an earlier version refuses to start on a database
migrated past it, as with every migration: upgrade every server before one
of them restarts on the old version.

- The role in the DSN needs to create tables and indexes in its schema the
  first time; after that it only reads and writes rows.
- A migration that does not finish within 30 seconds fails the open with
  `OpenError` (code -32014); another process holding the migration lock is the
  usual cause.
- Migrations are only ever added. Do not edit or drop `linch_` tables by hand
  while runs are live.
- Payloads are stored as `TEXT`, not `JSONB`, so the bytes the model saw are
  the bytes read back.
- The client has no call that deletes runs or sessions. The server's
  [retention](#retention) does, when you turn it on.

Schema details, write paths and the refusal rules are in the
[architecture stores page](../architecture/stores.md#postgres-stores).

### Connections and pools

- One server process keeps one pool per connection target, shared by every
  agent opened on it. The target is the host or socket, port, database,
  user, password, startup `options`, `sslmode` and TLS files, as the store
  reads the DSN. DSNs that differ only in the order of their parameters, or
  in others such as `application_name`, share one pool, and its connections
  keep the `application_name` of the open that connected it.
- Opening gives up after 5 seconds when the database cannot be reached
  (`OpenError`, code -32014, naming the host and port it tried), instead of
  retrying for longer.
- A pool holds at most `max_connections` connections (at least 1). When
  absent, the default follows the machine: its number of cores, but no fewer
  than 10 and no more than 32 (32 on a 64-core machine). Two numbers set
  these bounds:
  - A run that commits back to back uses about two connections, so plan about
    two connections per run in flight at once, per pool. Too small a pool makes
    runs wait for a connection: in the 2000-session bench, a pool of 10 added
    3 to 4 s to each turn, the default of 32 (on 64 cores) about 0.6 s, and a
    pool of 40 about 0.4 s.
  - Postgres's own `max_connections` (100 by default) is shared by every pool
    of every process on the database, workers and admin tools included. That
    is why the default stops at 32: three server processes at the default
    still fit. Four do not: runs then fail with `pool timed out`. With N
    processes (sidecars) on one database, set each one's
    `stores.max_connections` to about (the database's limit minus headroom)
    / N, for example 20 for four sidecars on a limit of 100. Raise
    `stores.max_connections` past 32 only after raising the database's
    limit, or put a pooler such as PgBouncer in front.
- The open that connects a pool sets its size; agents that share the pool
  share that size. Several server processes on one database each have their
  own pools.
- On a `linch-server` with tenants, each tenant has pools of its own: two
  tenants on one target never share a pool, so neither sets the other's
  size. A tenant's `stores.max_connections` may be at most its
  `max_pool_connections` (the default pool size when unset); above it,
  `open_agent` is refused (`NotAllowed`, `kind == "pool_connections"`). A
  config without `max_connections` gets the default size or the cap,
  whichever is smaller ([Server](./server.md#config)).
- Each Postgres store (one per open agent) also keeps **one connection of its
  own** outside the pool, for renewing and releasing run leases ([Run
  leases](#run-leases)), so a run's heartbeat never queues behind run work.
  It opens on the first renewal and counts against Postgres's own
  `max_connections`: budget `max_connections` plus 1 for every agent opened on
  a pool, and one more for a worker's job heartbeat. (A workflow that is held
  on a server takes one more backend each, outside the pool: N running
  workflows are N extra connections.)
- A connection that sat idle for a second or more is checked before its next
  write and replaced if the database closed it (a restart, a failover, an
  idle timeout), so a run that waited on a long model call still writes its
  answer. A connection that fails in the middle of a write stops the run at
  its last durable state; resume it.

### Run leases

On Postgres, one server drives a run at a time. The agent that starts,
resumes, approves or answers a run holds its lease and renews it every third
of the lease's length while it drives the run, so a long model call or tool
keeps it. Until that run stops, ends, or waits for an approval or an
answer, another agent's `resume`, `approve` or `answer` is refused with
`RunLeased` (code -32033) and writes nothing; `.owner` is the id of the server
that holds it (opaque: 8 random hex characters, or the server's `server_id`;
[Which server drives a run](./server.md#which-server-drives-a-run)) and
`.expires_in` the seconds its lease has left.

- A run whose stream ended with an error, or whose connection dropped, is
  released at once: another server can resume it right away. So are the
  runs of a server that exits normally (its input closed, `async with
  Server()` left): it waits up to 5 s for those releases before it exits.
- A server that crashed or hung releases nothing. Its runs are refused for
  up to the lease's length (30 s by default), then any server can resume
  them.
- If a server stalls longer than its lease (a frozen process, a lost network
  to the database), another server can take its runs over. When it wakes, its
  next write is refused and its run ends with `LeaseLost` (code -32022)
  without writing anything: the run's new driver owns it.
- The lease is renewed for as long as the run lives on its server, a third of
  the lease apart, by a task of its own: it does not wait for the run, for
  the stream to be read, or for the pool. Three things used to cost a run its
  lease while its server was healthy, and no longer do:
  - A pool whose connections are all busy (a row lock, a slow disk). A
    renewal uses the store's own connection, and gives up after a third of the
    lease (also as the statement's `statement_timeout`), so one stuck renewal
    costs one beat; the next is due a third of the lease after the previous
    one began. A renewal that fails for another reason (the database is
    unreachable) is tried again at the next beat, on a new connection if the
    old one broke.
  - A client that stops reading a run's events, long enough for the server's
    queue to fill ([backpressure](../architecture/rpc.md#backpressure)). The
    run pauses on that client but stays its server's: no other server can
    resume it while the connection lives, and it goes on when the client reads
    again. A HTTP connection that reads nothing is ended by the
    [liveness](./server.md#liveness) ping, which drops its runs (resumable
    from their last durable state); a Unix socket or stdio client that never
    reads again holds its run until it closes.
  - A stream nobody polls (a library host that stopped reading): the run does
    not advance, but it keeps its lease until its stream is dropped.
- `stores.lease_ttl_ms` sets the lease's length, at least 1000. Shorter means
  a crashed server's runs are free sooner, against one more small write per
  running run each third of it.
- The same agent handle never waits on its own lease: a run whose stream you
  dropped resumes at once through it.

```python
{"type": "postgres", "dsn": "postgres://user:pass@db:5432/linch", "lease_ttl_ms": 10000}
```

Memory stores have no leases: each process has its own runs, and the agent
that holds them is the only one that can drive them.

Memory stores keep every run's history for as long as the agent lives, so the
server's memory grows with the runs: about 110 KB per run in a measurement of
a release server (100 runs of one `Bash` call each, 24 MB after 25 runs and 32
MB after 100; closing the agent did not shrink the process). Use Postgres
stores for an agent that lives long or serves many runs.

## Retention

Postgres keeps every run, event and session until something deletes it. A
schedule that fires every minute adds a run and a session a minute (1440 a
day), and each tool turn adds about 8 event rows. Retention is the server's
own, opt-in way to bound that. It is off unless the `worker` section of the
server config has a `retention` object:

```json
{"worker": {"dsn": {"env": "LINCH_DSN"},
            "retention": {"terminal_runs_days": 30, "idle_sessions_days": 90}}}
```

| Field | Default | Meaning |
|---|---|---|
| `terminal_runs_days` | none | Delete a run (its record and every event) that ended more than this many days ago |
| `idle_sessions_days` | none | Delete a session (its log, tasks and task edges) that nothing wrote to for this many days |
| `tombstone_days` | 30 | How long a deleted run is remembered (see [what a client sees](#what-a-client-sees)) |
| `batch_size` | 500 | The most runs or sessions one transaction deletes (1 to 10000) |
| `interval_secs` | 3600 | The least time between two passes (at least 60) |

At least one of the two windows is required, a window is whole days (not 0),
and a bad value stops the server at startup. A worker runs a pass in a task of
its own, so a long pass never delays a poll, and the fleet runs one pass at a
time: a pass that finds another server's pass running stops and tries again at
its next interval. The worker needs a `worker` section; a server without one
deletes nothing.

### What is deleted and what is kept

The two windows are separate decisions. A session may be continued by a
person long after its runs ended, so deleting the runs of an old session does
not delete the session, and the other way round.

| Kept, whatever the window | Why |
|---|---|
| A run that is not terminal (`pending`, `running`, `awaiting_approval`, `awaiting_input`) | A suspended run waits for a person for as long as it takes; deleting it would end it without an answer. The delete itself checks the status again |
| A child run (a subagent's) while its parent run is not terminal | The parent reads its children's events when it resumes; without them it would start the subagent again |
| A workflow's agent run and session (`wf-` ids) | A resumed workflow reads the run when its journal has no record of the call yet; without it the agent would run again |
| A run a queued or running job still names | A retried start job that finds its run missing starts another one |
| A session with a run that is not terminal | A run in it still reads the log |

Deleted rows go in batches, each its own transaction: a run with its events
(and a tombstone), a session with its log, its tasks and the task edges. A
crash between batches leaves every run and session whole or gone. The pass
walks each table once in key order, so rows it cannot delete are not read
again until the next pass.

Nothing else is touched. The job history and failed jobs keep their own
`history_retention_ms`; schedules, definitions, files, long-term memories,
mailbox messages, the tool cache and workflow journals are not part of
retention. A job's history row survives the run it names, so `job.list` is
the same before and after a pass, and a schedule's next fire is not moved.

Retention deletes audit data: after the window nobody can read a run's events
or report. Choose a window that covers what you need to look back on, or copy
what you must keep before it ends.

A session deleted for being idle is gone: starting a run in that session id
afterwards starts an empty session. A session's activity is its own: a parent
session that is still in use does not keep the sessions of its subagents
alive, so keep `idle_sessions_days` longer than the longest gap after which
anyone returns.

### What a client sees

A read, resume, approve, answer, steer, cancel or report of a deleted run is
`RunPruned` (code -32050, a `RunNotFound` subclass; `data.run_id`), and not a
plain "not found", for `tombstone_days`. After that the server has forgotten
it and the run reads as `RunNotFound`. A wrong id is always `RunNotFound`.

### Upgrading

Migration 10 adds `linch_runs.ended_at` (set by the commit that makes a run
terminal), `linch_sessions.updated_at` (set by creating a session and by every
append) and `linch_pruned_runs`. It rewrites no row, so it takes about as long
on a database of millions of runs as on an empty one (the time of the two
index builds is under [Operations](#operations)). The runs that were terminal
before it have no end time: the first pass that has `terminal_runs_days` gives
each one the time of that pass, so its window starts there, and every
existing session's clock starts at the migration. The first rows go one
window after you turn retention on, not at the upgrade.

### Operations

Measured on a shared 64-core host with a Postgres 16.15 of default settings
(`shared_buffers` 128 MB) on the same machine, on a database of 520k runs (4.2M events), 516k sessions
(1.6M log entries) and 300k history rows, with the retention pass deleting
while 4 clients started scripted runs on the same schema:

- Before any deletion the tables held 2.0 GB of events (4.0M rows, 500 bytes a
  row with its index), 0.3 GB of runs (505k rows, 600 bytes), 0.09 GB of
  sessions (500k rows, 180 bytes) and 0.47 GB of log entries (1.5M rows, 310
  bytes). A text-only scripted run (8 events, 1 session, 3 entries) therefore
  leaves about 5.7 KB: a schedule that fires every minute adds 8 MB a day.
- A pass deleted 67k runs with their 536k events and 66k sessions with their
  214k entries (about 0.9M rows) in 187 s, 4.7k rows/s, with a 500-row batch
  and the pause below. A pass over 263k runs and 259k sessions (3.4M rows)
  took 24 minutes with the pause, on a busier host (a single sample; a
  concurrent job of another user was running).
- Each batch is followed by a pause as long as the batch took, so a pass
  keeps the database at most half busy for it. The run throughput of the 4
  clients (batches of 100 runs) averaged 58 runs/s during the pass and 67
  runs/s in a separate control window with no pass (single samples, on a
  database a quarter of the size, with 3 to 123 runs/s between batches:
  indicative, not a bound), and every run succeeded. Stamping 500k runs
  without a pause took the same clients down to 24 runs/s on average, which
  is why the pass pauses.
- Picking a batch of runs reads 500 index entries in about 5 ms at any
  depth into the pass; a pass with nothing to delete costs 0.05 ms. Picking a
  batch of sessions is 7 ms; a session scan with nothing to delete is a full
  key-order scan, 0.45 s at 516k sessions, once per pass.
- The migration rewrites no row (an open of 518k runs migrated in 1.3 s). A
  partial index on a terminal run costs about 77 bytes a row (39 MB for 500k
  runs) and is written once per run, when it ends.
- Deleting does not shrink the files: Postgres reuses the freed pages after
  autovacuum has seen them, and `VACUUM FULL` or `pg_repack` gives the space
  back to the operating system. A pass makes dead rows at its batch speed,
  so on a table of millions of rows raise autovacuum's scale for the first
  pass, or run `VACUUM (ANALYZE)` after it.
- A larger `batch_size` deletes faster and holds the locks of the rows it
  deletes for longer; 500 is the default.

Terminal rows did not measurably slow a run down: with these 520k runs and
4.2M events in the schema and nothing deleted, the scripted-run throughput at
1, 4 and 16 clients (medians of three 600-run batches, two series each) was
43, 159 to 221 and 539 to 728 runs/s against 44 to 46, 187 to 204 and 583 to
649 runs/s on an empty schema: the series overlap, and the host's noise is
larger than any difference. (A first series of 200-run batches read 481 runs/s
at 16 clients against 709 on the empty schema; the 600-run series did not
repeat it.) The claim, lease, expiry and due-fire queries kept their plans
(0.02 to 1.4 ms), because they read the partial indexes over live rows; a
resume of a run, from a spawned server to its result, took 80 ms on both. The
cost of keeping every run is the disk (the sizes above) and the scans that
read a whole namespace, such as `job.list` (205 ms at 180k history rows),
not the run path.

## The virtual filesystem

With `filesystem` on, where the files live follows the stores:

```python
import asyncio
import os

from linch_rust import AgentConfig, Server, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

TURNS = [
    tool_turn("c1", "write_file", {"path": "/plan.md", "content": "1. ship"}),
    text_turn("Wrote the plan."),
    tool_turn("c2", "read_file", {"path": "/plan.md"}),
    text_turn("Read the plan."),
]


def config(stores: dict) -> AgentConfig:
    return {
        "namespace": "files-demo",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": TURNS},
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
        "filesystem": {},
        "stores": stores,
    }


async def main() -> None:
    for stores in ({"type": "memory"}, {"type": "postgres", "dsn": os.environ["DATABASE_URL"]}):
        async with Server() as server:
            agent = await server.open_agent(config(stores))
            await (await agent.run("writer", "Write the plan")).result()
            async for envelope in await agent.run("reader", "Read the plan"):
                if isinstance(envelope.event, ToolCallEndEvent):
                    print(stores["type"], "|", envelope.event.result["content"][0]["text"])


asyncio.run(main())
```

```text
memory | File not found: '/plan.md'. Use ls() to see available files.
postgres | 1. ship
```

With memory stores each session has its own files, gone with the server; with
Postgres every session of the namespace shares them (`linch_files`), and a run
resumed in another process still finds an offloaded result.

## Related pages

- [Runs](./runs.md#what-is-durable-and-when): what is written when.
- [Agent config](./agent.md): `namespace`, `stores`, `filesystem`.
- [Troubleshooting](./troubleshooting.md#postgres): connection and migration
  errors.
- [Architecture: stores](../architecture/stores.md): contracts, schema, write
  paths and pool sizing.

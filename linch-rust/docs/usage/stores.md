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
wait on a database lock. Applied versions are recorded in `linch_migrations`,
so your own migration tool's table in the same schema does not collide.

| Table | Holds |
|---|---|
| `linch_runs`, `linch_run_events` | Each run's status and checkpoint, and its durable events (what `agent.events` returns) |
| `linch_sessions`, `linch_session_entries` | Each session's conversation log |
| `linch_files` | The virtual filesystem, per namespace |
| `linch_tasks`, `linch_task_edges` | Session tasks and their dependencies |

- The role in the DSN needs to create tables and indexes in its schema the
  first time; after that it only reads and writes rows.
- A migration that does not finish within 30 seconds fails the open with
  `OpenError` (code -32014); another process holding the migration lock is the
  usual cause.
- Migrations are only ever added. Do not edit or drop `linch_` tables by hand
  while runs are live.
- Payloads are stored as `TEXT`, not `JSONB`, so the bytes the model saw are
  the bytes read back.
- The client has no call that deletes runs or sessions.

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
    still fit. Raise `stores.max_connections` past 32 only after raising
    the database's limit, or put a pooler such as PgBouncer in front.
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
- A connection that sat idle for a second or more is checked before its next
  write and replaced if the database closed it (a restart, a failover, an
  idle timeout), so a run that waited on a long model call still writes its
  answer. A connection that fails in the middle of a write stops the run at
  its last durable state; resume it.

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

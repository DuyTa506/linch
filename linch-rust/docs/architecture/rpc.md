# RPC protocol and server

> Part of the [linch-rust architecture guide](./README.md).

`linch-rpc` holds the JSON-RPC 2.0 protocol of Linch: its message types, a
server for one connection, and a Rust client. `linch-server` is the
sidecar binary that serves it on stdio, a Unix socket, or HTTP with a
WebSocket, with tenants and allowlists from a config file. A client opens
agents from configs, starts, approves, cancels and replays runs, and gets
every envelope of a run in a notification. Everything inside a turn runs in
the server; it calls back into the client only for host tools and for the
callbacks the client declared.

The protocol itself is specified in the crate docs of `linch-rpc`
(`crates/linch-rpc/src/lib.rs:1-372`): framing, every method, server
request and notification, and the error table. This page explains how the
code implements it, where its limits and checks live, and how to change
it. The [server usage page](../usage/server.md) shows how to run it and
connect from Python.

Python Linch has no counterpart: it is a library that hosts embed in their
own process, so it has no wire protocol, tenants or allowlists. The Python
package `linch_rust` (`python/src/linch_rust/`) is a client of this server.

## Crate layout

| File | Holds |
|---|---|
| `crates/linch-rpc/src/lib.rs` | The protocol document (crate docs) and the public exports |
| `crates/linch-rpc/src/protocol.rs`, `protocol/{open,call,hook}.rs` | Params and results of every method, `method::` names, `tool_code` |
| `crates/linch-rpc/src/message.rs` | Strict reading of a JSON-RPC request, response or notification |
| `crates/linch-rpc/src/lines.rs` | The line codec, which keeps the head of an oversized line to find its `id` |
| `crates/linch-rpc/src/error.rs` | Error codes and `ErrorObject`s from core and facade errors |
| `crates/linch-rpc/src/access.rs`, `access/{admit,allowlists}.rs` | `Access`, `Tenant`, `Tokens`, `Allowlists` and the admission checks |
| `crates/linch-rpc/src/server.rs` | One connection's state, the read loop and the method dispatch |
| `crates/linch-rpc/src/server/connection.rs` | The transport entry points and the writer task |
| `crates/linch-rpc/src/server/outgoing.rs` | How a run's envelopes join into one `run.event` |
| `crates/linch-rpc/src/server/login.rs` | The socket's `connection.auth` |
| `crates/linch-rpc/src/server/agents.rs`, `server/run.rs` | The method handlers |
| `crates/linch-rpc/src/server/peer.rs`, `server/host.rs`, `server/callback.rs` | Requests from the server: host tools (`tool.call`) and callbacks (`hook.call`) |
| `crates/linch-rpc/src/client.rs` | The Rust `Client` |
| `crates/linch-server/src/main.rs` | The binary: stdio, or the listeners with `--config FILE` |
| `crates/linch-server/src/{config,listen,http}.rs` | The config, the Unix socket and the HTTP transport |
| `crates/linch/src/pools.rs` | `PgPools`: Postgres pools shared by every connection of a process |

## Methods and messages

Every method name is a `method::` constant (`crates/linch-rpc/src/protocol.rs:25-55`),
and `Server::dispatch` (`crates/linch-rpc/src/server.rs:293-311`) maps each
to its handler. Params types refuse unknown fields; `OpenParams` takes
`tools` and `callbacks` out and reads the rest as a strict `AgentConfig`
(`crates/linch-rpc/src/protocol/open.rs:13-28`).

| Method | Params | Handler | Result |
|---|---|---|---|
| `connection.auth` | `AuthParams` (`protocol.rs:62`) | `login` (`crates/linch-rpc/src/server/login.rs:24`), first message on the socket only; elsewhere `-32600` | `Authenticated` `{tenant}` |
| `agent.open` | `OpenParams` | `Server::open` (`crates/linch-rpc/src/server/agents.rs:19-48`) | `AgentOpened` `{agent, contract, skipped?}` |
| `agent.close` | `CloseParams` | `agents.rs:51-75`; `-32002` while the connection drives a run of it | `{}` once its MCP servers are gone |
| `session.close` | `SessionCloseParams` | `agents.rs:76-85`: `Agent::close_session` | `{}` |
| `run.start` | `StartParams` (`protocol.rs:154`) | `Server::start` (`crates/linch-rpc/src/server/run.rs:20-43`) | `RunAccepted` `{run_id}`, then notifications |
| `run.resume` | `ResumeParams` (`protocol.rs:178`) | `run.rs:45-50`, through `claim` | the same |
| `run.approve` | `ApproveParams` (`protocol.rs:191`) | `run.rs:52-58`, through `claim` | the same |
| `run.cancel` | `CancelParams` | `run.rs:60-69`: cancels a run this connection drives | `{cancelled}` |
| `run.events` | `EventsParams` | `agents.rs:87-94`: `Agent::events` | `{events}`, the durable envelopes after `after_seq` |

The server sends two notifications. `run.event` `{run_id, envelopes}`
(`RunEvent`, `protocol.rs:256`) carries one or more consecutive envelopes
of one run; `run.end` `{run_id, error?}` (`RunEnd`, `protocol.rs:266`) is
the last message of a run. It also sends requests of its own: `tool.call`
for a host tool (`crates/linch-rpc/src/protocol/call.rs:12`,
`crates/linch-rpc/src/server/host.rs:70`) and `hook.call` for a callback
(`crates/linch-rpc/src/protocol/hook.rs:46`,
`crates/linch-rpc/src/server/callback.rs:128`). Their ids are `srv-<n>`, so
they never equal a client's numeric ids; a call that stops waiting sends
`$/cancelRequest` `{id}` and drops a late answer
(`crates/linch-rpc/src/server/peer.rs:1-7`).

### Errors

`ErrorObject` is `{code, message, data?}`; the full table is in the crate
docs (`lib.rs:326-362`) and the codes in `crates/linch-rpc/src/error.rs:24-100`.

| Codes | From |
|---|---|
| -32700, -32600, -32601, -32602, -32603 | JSON-RPC: the line, the request object, the method, the params; a panic |
| -32001, -32002 | An unknown agent handle; a run the connection already drives, or `agent.close` of an agent with one |
| -32003 | A socket's first message is not `connection.auth` with a known token |
| -32004 | A tenant check refused; `data.kind` names it |
| -32005 | A cap is reached and nothing started; `data: {kind, max}`, `kind` one of `runs`, `agents`, `connections`, `logins` (`limit_reached`, `error.rs:126-131`) |
| -32010 to -32015 | `agent.open`: `BuildError`s, provider, store and MCP server unavailable |
| -32020 to -32030 | A run, in `run.end`, and `run.events`: store errors, `RunError`s, session busy |

`RunError::ContractMismatch {expected, found}` names its contracts from the
agent's side: `expected` is the contract of the agent that tried to
continue the run, `found` the one stored with the run. The wire turns them
around, so that `data.expected` is the run's contract and `data.found` the
agent's, as the message text reads (`error.rs:157-165`, tested at
`error.rs:222-242`).

The socket login answers its refusals under different ids
(`login.rs:33-85`): `-32005` `kind: logins` under a null id before anything
is read; `-32003` under the request's id, or null when the first message
has none or none came within the login wait; `-32005` `kind: connections` under the `connection.auth` id.
The Python client reads the first line it gets as its login's answer,
whatever the id (`python/src/linch_rust/_remote.py:112-142`).

The Python client maps codes to exceptions in `_BY_CODE`
(`python/src/linch_rust/errors.py:158-170`), with ranges for the rest
(`errors.py:173-186`); `python/tests/test_protocol.py:196` pins the map.

## Inside a connection

Each connection is one `Server` (`server.rs:53-78`): its tables of open
agents, driven runs and parked runs under one lock, so `agent.close` cannot
miss a run that starts while it checks; its outbox; its peer for server
requests; and its tenant, `None` for a local client.

1. **Transport.** `serve_lines` (`crates/linch-rpc/src/server/connection.rs:83-108`)
   frames a byte stream into lines; `serve_messages` (`connection.rs:125-148`)
   takes one message per frame. Both hand `Inbound` items (`connection.rs:151-159`)
   to `connection` (`connection.rs:161-219`), which spawns the writer task,
   runs the login when access is `Login`, and reads until the input ends
   or `stop` fires.
2. **Read loop.** `Server::receive` (`server.rs:170-206`) parses a line,
   hands a client's answer to the server request it names, and reads
   anything else as a request (`Request::from_value`,
   `crates/linch-rpc/src/message.rs:108`). A request without an `id` is a
   notification and is dropped. Each call then takes one of 64 permits
   (`MAX_REQUESTS`, `server.rs:49`); the loop awaits it, so the 65th
   request stops reading until a call ends. The call runs in its own task,
   so a slow `agent.open` never holds up a `run.cancel`.
3. **Dispatch.** `Server::handle` (`server.rs:264-291`) catches a panic as
   `-32603`. A run method returns its stream: the `RunAccepted` result is
   queued first, then a task drives the stream.
4. **A run.** `Server::drive` (`run.rs:159-196`) encodes each envelope once,
   to a raw JSON value, and queues it. When the stream ends it drops it,
   removes the run from the tables (keeping its place when it parked, see
   [limits](#limits)) and queues `run.end`. A run's panic ends it with
   `-32603`, at its last durable state.
5. **Writer.** `write_messages` (`connection.rs:223-252`) takes everything
   already queued, joins consecutive envelopes of one run into one
   `run.event` (`Batch::push`, `crates/linch-rpc/src/server/outgoing.rs:74-83`)
   up to 64 KiB (`outgoing.rs:21`), and flushes once nothing waits.
6. **Server requests.** `Peer::call` (`peer.rs:92-139`) queues the request
   and waits for its answer, its timeout or its cancel; waiting for room in
   the outbox counts against both.
7. **End of input.** `Server::stop` (`server.rs:149-168`) lets the calls
   already read finish, drops every run where it stands (a durable run stays
   resumable), fails the waiting server requests, then closes every agent's
   MCP servers. The writer drains the queue and the connection returns.

### Ordering

- A run's `RunAccepted` is queued before the task that drives it is
  spawned (`server.rs:277-288`), so it reaches the client before any
  notification of that run. The Python client relies on it: it files a
  run's notifications under the run id it read from that result
  (`python/src/linch_rust/_rpc.py:254-265`).
- Every message goes through one queue to one writer, so the wire keeps the
  queue's order. A batch joins only what is already queued, and any other
  message or another run's envelope closes it (`outgoing.rs:1-10`): a
  `tool.call` still follows its call's `tool_call_start`.
- Durable envelopes reach the server's stream only once they are durable;
  stream-only ones come at once with the last durable seq
  ([persistence classes](./events.md#persistence-classes)).
- `drive` drops the stream before `run.end` (`run.rs:183-185`), so the run
  is at its last durable state when the client may resume it, and removes
  the run from the tables under the lock, so no approve claims it in
  between.
- Answers to calls in flight come in any order; match them by `id`.

### Backpressure

There is no buffer that grows without bound on the server:

1. The outbox holds 1024 messages (`OUTBOX_CAPACITY`, `connection.rs:31`).
   The writer waits on the transport, so a client that reads slowly fills
   it.
2. A run's task waits for room before it polls the run's stream again, and
   a run advances only while its stream is polled (the core spawns no task
   for it), so the run pauses. A server request waiting for room counts it
   against its timeout.
3. Reading stops at 64 calls in flight.
4. The Rust client keeps at most 1024 unread notifications
   (`NOTIFICATION_CAPACITY`, `crates/linch-rpc/src/client.rs:29`), then
   stops reading, so answers wait behind them too (`client.rs:103-110`).
   The Python client does the same with its own 1024
   (`NOTIFICATION_CAPACITY`, `python/src/linch_rust/_rpc.py:46`): each
   unread `run.event` holds a place across the connection's runs, and the
   reader waits for one before it queues the next (`RunQueue`,
   `_rpc.py:60-96`). `Run.cancel` keeps reading its run while it waits for
   its answer (`python/src/linch_rust/run.py:102-121`); a run nobody reads
   while its events keep coming stalls the connection.

`Outbox::send_now` (`connection.rs:267-277`), for code that cannot wait,
hands a message to a task when the queue is full.

## Transports

| Transport | Entry | Access | Runtime |
|---|---|---|---|
| stdio | `linch-server` with no arguments; `linch_rpc::serve` (`connection.rs:43-57`) | `Access::Local`: no login, no tenant, no caps | Current-thread (`crates/linch-server/src/main.rs:49-55`): one client, so handing an envelope between its tasks wakes no other thread |
| Unix socket | `--config FILE` with `socket`; `Listeners` (`crates/linch-server/src/listen.rs:42-101`) | `Access::Login`: the first message is `connection.auth` | Multi-thread |
| HTTP with WebSocket | `--config FILE` with `http`; `crates/linch-server/src/http.rs:77-116` | `Access::Tenant`: the bearer header is checked before the upgrade | Multi-thread |

**stdio.** On Linux the server reopens its pipes through
`/proc/self/fd/0` and `/proc/self/fd/1` and drives those in non-blocking
mode (`main.rs:88-123`). Non-blocking is a flag of the open file
description, which fds 0 and 1 share with every process holding the same
pipe; new descriptions keep it from leaking into a shell's or a
supervisor's other readers. Anything that is not a pipe uses tokio's
`stdin()` and `stdout()`.

**Unix socket.** A socket file left by a stopped server is replaced, and
one a server answers on is refused (`bind_socket`, `listen.rs:105-122`).
Each connection waits for its login with one of 32 places
(`MAX_PENDING_LOGINS`, `crates/linch-rpc/src/access.rs:125-129`): each may
buffer a 16 MiB line for 10 s, so the places bound that to 512 MiB. One
more gets `-32005` at once. A login has 10 s (`LOGIN_TIMEOUT`,
`login.rs:17`); silence past it gets `-32003` and the connection closes
(`login.rs:42-54`).

**HTTP.** One route, `GET /rpc` (`http.rs:30`). Before its upgrade a
connection holds one of 32 places across the server
(`MAX_PENDING_UPGRADES`, `http.rs:39-44`); one more is closed unread. Each
request head has 10 s (`HEADER_READ_TIMEOUT`, `http.rs:32-37`), then the
connection closes without an answer. `upgrade` (`http.rs:135-171`) checks
the bearer token first (`401` with `WWW-Authenticate: Bearer`), then takes
the tenant's connection place (`429`), then answers `101` naming the
tenant in `Linch-Tenant`. A WebSocket message may be 64 MiB
(`MAX_MESSAGE_BYTES`, `http.rs:50-53`) before the socket closes; one over
16 MiB is refused by the core like an oversized line. The server speaks
plain HTTP: put TLS in front of it or listen on loopback.

All listeners of one server share one `PgPools` (`listen.rs:77-101`).

## Tenants and allowlists

`ServerConfig::from_json` (`crates/linch-server/src/config.rs:153`) reads the
config, refusing unknown fields and a cap of 0; `tokens`
(`config.rs:187`) reads each token inline or from the environment;
`AllowConfig::build` (`config.rs:220`) makes the `Allowlists`.

- A `Tenant` (`access.rs:40-115`) has a name of 1 to 64 ASCII letters,
  digits, `.`, `_` or `-` (never `/`), its allowlists, and one semaphore
  each for runs, agents and connections, shared by all its connections.
- `Tokens` (`access.rs:131-196`) keep each token as a SHA-256 digest and
  compare a presented one with every digest in constant time. No token is
  logged or echoed.
- `Tenant::admit` (`crates/linch-rpc/src/access/admit.rs:20-113`) checks an
  `agent.open` config before anything starts: no key is read, no MCP
  process started, no database connected for a refused one. Each refusal is
  `-32004` with `data.kind`:

| Check | `kind` | Where |
|---|---|---|
| The namespace is the tenant's name or starts with `<tenant>/` | `namespace` | `admit.rs:26-36` |
| The scripted provider only if allowed; a client-chosen `base_url` only at an allowed origin, since the server's key goes there | `provider`, `provider_base_url` | `admit.rs:131-147`, `admit.rs:218-232` |
| The permission mode is in the tenant's list; `tasks` and `deep_agent`, which auto-allow the task tools, need `skip_dangerous` there | `permission_mode`, `auto_allow` | `admit.rs:39-57` |
| `auto_allow` entries and rules that allow only when the tenant's modes include `skip_dangerous` | `auto_allow`, `permission_rule` | `admit_grants`, `admit.rs:188-212` |
| `project_root` and `workspace` are existing directories under an allowed root, replaced by their canonical paths | `project_root`, `workspace` | `admit.rs:58-70`, `admit_dir` at `admit.rs:149-158` |
| With skills or subagents on, the config directory is an existing directory under an allowed root, or the default `.linch` there or missing | `config_dir` | `admit.rs:71-75`, `admit.rs:160-186` |
| `Bash` only when the tenant allows exec tools; otherwise it is turned off, not refused | none | `admit.rs:76` |
| A Postgres DSN only for an allowed target | `postgres` | `admit.rs:77-83` |
| An MCP stdio server only with an entry's `command` and `args`, setting only the `env` names it lists; an MCP URL only at an allowed origin | `mcp_command`, `mcp_env`, `mcp_url` | `admit.rs:84-111` |

A run method's `principal`, when given, must name the tenant
(`admit_principal`, `admit.rs:115-129`); the server does not fill one in.

**Postgres.** `PgTarget::parse` (`crates/linch-rpc/src/access/allowlists.rs:134-171`)
reads a DSN the way the store connects with it, through sqlx's
`PgConnectOptions`, so a `?host=` or `?user=` override counts and a missing
user or `sslmode` takes the server's default. An entry allows a DSN with
the same host, port, socket, database, user, startup `options` and TLS
files, and an `sslmode` at least as strict as its own, ranked from
`disable` (0) to `verify-full` (5) (`PgTarget::allows`, `allowlists.rs:173-194`).

The facade shares one pool per target across a server's connections
(`crates/linch/src/pools.rs:19-38`). Its key (`pools.rs:44-73`) is sqlx's
normalized URL of the DSN plus its startup `options`, which that URL leaves
out and which can set the `search_path`; DSNs that differ only in parameter
order or in settings sqlx ignores share a pool. A pool takes its size from
the open that connected it (`pools.rs:93-109`); the connect gives up after
5 s and the migrations after 30 s (`pools.rs:136-154`).

**MCP commands.** An `McpCommand` (`allowlists.rs:51-71`) may not list
`PATH`, `HOME` or the variables known to choose what code a program loads
(`CODE_LOADING_ENV` and its prefixes, `allowlists.rs:73-110`). The list is
best effort.

## Limits

| Limit | Value | Where | Past it |
|---|---|---|---|
| `max_connections` per tenant | 8 unless set (`allowlists.rs:23`) | `Tenant::connect` (`access.rs:87-104`); the connection holds the place until it ends (`connection.rs:177-188`) | Socket: `-32005` under the `connection.auth` id; HTTP: `429` |
| `max_runs` per tenant | 16 unless set (`allowlists.rs:16`) | `run_place`, taken in `start` and `claim` (`run.rs:152-157`) and held by the driven run | `-32005`, nothing starts |
| `max_agents` per tenant | 16 unless set (`allowlists.rs:28`) | Taken in `open` after the checks (`agents.rs:19-26`), freed once the agent's MCP servers are gone (`server.rs:103-110`) | `-32005`, before any MCP server starts |
| Calls in flight per connection | 64 | `server.rs:49` | Reading waits |
| Logins waiting | 32 per `Tokens` | `access.rs:125-129` | `-32005` `kind: logins`, then close |
| HTTP upgrades waiting | 32 per server | `http.rs:39-44` | Closed unread |
| One line or message | 16 MiB (`connection.rs:28`); its first 64 KiB are kept to find its `id` (`crates/linch-rpc/src/lines.rs:18`) | `Server::too_long` (`server.rs:231-250`) | `-32600`, under the request's id when the head shows it; an answer fails its server request |
| Login wait, HTTP head | 10 s each | `login.rs:17`, `http.rs:37` | The login gets `-32003`, then the connection closes; the HTTP connection closes |

A local (stdio) connection has none of the tenant caps.

**A parked run keeps its place.** A run that suspends for approval while
its background subagents run goes on spending the server's provider
streams, so when its stream ends `keep_place` (`run.rs:103-143`) moves its
run place into the connection's `parked` table and waits for
`Agent::parked_children` ([the run family](./skills-subagents.md#the-run-family)).
The place is freed once the workers end; an approve or resume of the run on
this connection takes it over instead of a new one (`run.rs:84-89`).
Either way the run holds one place, so a tenant's running runs have at most
`max_runs × (1 + max_background)` provider streams.

## The clients

The Rust `Client` (`client.rs:97-101`) has one generic method,
`call<P, T>(method, params)` (`client.rs:192`), with no wrapper per method.
`Client::connect_with` (`client.rs:141`) answers server requests with a
`RequestHandler` (`client.rs:57`); `Client::in_process` (`client.rs:174`)
starts a server on in-memory pipes, as the tests do.

The Python client's `Connection` (`python/src/linch_rust/_rpc.py:160`) has
one reader task. It resolves answers by id, files `run.event` envelopes and
`run.end` under their run (`_rpc.py:232-252`), runs `tool.call` and
`hook.call` in a task each and cancels one on `$/cancelRequest`
(`_rpc.py:267-310`). `Agent` (`python/src/linch_rust/agent.py:50`) wraps the
methods: `run`, `resume` and `approve` through `_start` (`agent.py:203`),
and `close`, `close_session` and `events` (`agent.py:159-201`).

## Add an RPC method

| Step | Where | What catches a miss |
|---|---|---|
| 1. Name it | A `method::` constant in `protocol.rs:25-55` | Nothing; every other step uses it |
| 2. Its params and result | `protocol.rs` or a `protocol/` file, with `deny_unknown_fields` and doc comments | `-32602` for an unknown field only if the type refuses them |
| 3. The handler | An `impl Server` method in `server/agents.rs` or `server/run.rs`. Run the tenant checks (`admit_principal`, a place) before anything else, and return `Reply::Run` for a run | Tests in `crates/linch-rpc/tests/tenants.rs` and `limits.rs` for its checks |
| 4. Dispatch | An arm in `Server::dispatch` (`server.rs:293-311`) | `-32601` at run time |
| 5. Errors | A new code in `error.rs` with its conversion | `crates/linch-rpc/tests/wire.rs` for the wire shape |
| 6. The protocol document | The method in the crate docs' Methods section, a new code in its table (`lib.rs:118-244`, `lib.rs:326-362`) | Review only |
| 7. The Rust client | Nothing: `Client::call` is generic. A typed helper in `crates/linch-rpc/tests/common/mod.rs` if tests need one | Compilation of the tests |
| 8. The Python client | A method on `Agent` (`agent.py`), and for a new code an exception in `errors.py` and its `_BY_CODE` entry | `python/tests/test_protocol.py` |
| 9. Usage docs | `docs/usage/server.md` and the Python pages that call it | Review only |
| 10. Tests | `crates/linch-rpc/tests/<area>.rs` end to end through `Client::in_process` or the raw wire (`wire.rs`); a Python test against the built server | `cargo test -p linch-rpc`, `pytest` |

A field added to `AgentConfig` reaches `agent.open` with no change here;
`python/tests/test_config.py:52` checks that the Python typed config names
every field the server reads.

## Example

This runs against `linch-rpc` at this revision: it writes raw lines into an
in-process server, as a stdio client would, and prints both directions.

```rust
use linch::provider::scripted::TurnBuilder;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

#[tokio::main(flavor = "current_thread")]
async fn main() {
    // The server end of an in-memory pipe, as `linch-server` serves stdio.
    let (ours, theirs) = tokio::io::duplex(64 * 1024);
    let (server_in, server_out) = tokio::io::split(theirs);
    tokio::spawn(linch_rpc::serve(server_in, server_out));
    let (reader, mut writer) = tokio::io::split(ours);
    let mut lines = BufReader::new(reader).lines();
    let mut send = async |message: Value| {
        println!("> {message}");
        let line = format!("{message}\n");
        writer.write_all(line.as_bytes()).await.unwrap();
    };

    let turns = [TurnBuilder::new("m").text("hello").build()];
    let config = json!({
        "namespace": "rpc",
        "spec": {"model": "m"},
        "provider": {"id": "scripted", "turns": turns},
    });
    send(json!({"jsonrpc": "2.0", "id": 1, "method": "agent.open", "params": config})).await;
    let start = json!({"agent": "agent-1", "session_id": "s1", "prompt": "hi", "run_id": "r1"});
    send(json!({"jsonrpc": "2.0", "id": 2, "method": "run.start", "params": start})).await;
    let mut seen = Vec::new();
    while let Some(line) = lines.next_line().await.unwrap() {
        println!("< {line}");
        let message: Value = serde_json::from_str(&line).unwrap();
        let method = message["method"].as_str().unwrap_or("response").to_owned();
        seen.push(method.clone());
        if method == "run.end" {
            break;
        }
    }
    // The answer comes before the run's first notification.
    assert_eq!(seen[..2], ["response", "response"]);
    assert_eq!(seen.last().unwrap(), "run.end");

    // Handles are per connection; an unknown one is -32001.
    let stray = json!({"agent": "agent-9", "session_id": "s2", "prompt": "hi"});
    send(json!({"jsonrpc": "2.0", "id": 3, "method": "run.start", "params": stray})).await;
    let line = lines.next_line().await.unwrap().unwrap();
    println!("< {line}");
    let answer: Value = serde_json::from_str(&line).unwrap();
    assert_eq!((answer["id"].clone(), answer["error"]["code"].clone()), (json!(3), json!(-32001)));
}
```

Its output (`>` sent, `<` received). The scripted run was ready at once, so
all its envelopes were queued before the writer ran and went out in one
`run.event`; seq 5 is a journal-only `model_call_*` event, stored but not
delivered:

```text
> {"jsonrpc":"2.0","id":1,"method":"agent.open","params":{"namespace":"rpc","spec":{"model":"m"},"provider":{"id":"scripted","turns":[{"events":[{"type":"message_start","model":"m"},{"type":"text_delta","text":"hello"},{"type":"message_end","stop_reason":"end_turn","usage":{"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_creation_tokens":0}}]}]}}}
> {"jsonrpc":"2.0","id":2,"method":"run.start","params":{"agent":"agent-1","session_id":"s1","prompt":"hi","run_id":"r1"}}
< {"jsonrpc":"2.0","id":1,"result":{"agent":"agent-1","contract":"a80570d2a77d5d1600656180319cb890b0a31a879948779ba862b3303bfec300"}}
< {"jsonrpc":"2.0","id":2,"result":{"run_id":"r1"}}
< {"jsonrpc":"2.0","method":"run.event","params":{"run_id":"r1","envelopes":[{"v":1,"seq":1,"run_id":"r1","ts_ms":1794009573573,"event":{"session_id":"s1","run_id":"r1","model":"m","provider":"scripted","tools":[],"type":"init"}},{"v":1,"seq":2,"run_id":"r1","ts_ms":1794009573573,"event":{"message":{"role":"user","content":[{"type":"text","text":"hi"}]},"subtype":"prompt","type":"user"}},{"v":1,"seq":3,"run_id":"r1","ts_ms":1794009573573,"event":{"model":"m","system_blocks":0,"messages":1,"selected_tools":[],"budget":{},"metadata":{},"type":"context_build"}},{"v":1,"seq":4,"run_id":"r1","ts_ms":1794009573573,"event":{"delta":{"kind":"text","text":"hello"},"type":"partial_assistant"}},{"v":1,"seq":6,"run_id":"r1","ts_ms":1794009573573,"event":{"message":{"role":"assistant","content":[{"type":"text","text":"hello"}]},"stop_reason":"end_turn","type":"assistant"}},{"v":1,"seq":7,"run_id":"r1","ts_ms":1794009573573,"event":{"usage":{"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_creation_tokens":0},"cumulative":{"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_creation_tokens":0},"actual_model":"m","type":"usage"}},{"v":1,"seq":8,"run_id":"r1","ts_ms":1794009573574,"event":{"subtype":"success","stop_reason":"end_turn","total_usage":{"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_creation_tokens":0},"usage_complete":true,"duration_ms":0,"final_text":"hello","type":"result"}}]}}
< {"jsonrpc":"2.0","method":"run.end","params":{"run_id":"r1"}}
> {"jsonrpc":"2.0","id":3,"method":"run.start","params":{"agent":"agent-9","session_id":"s2","prompt":"hi"}}
< {"jsonrpc":"2.0","id":3,"error":{"code":-32001,"message":"no open agent \"agent-9\""}}
```

## Where Rust differs from Python

| Python Linch | linch-rust |
|---|---|
| A library in the host's process; nothing crosses a wire | A sidecar a client drives over JSON-RPC, or the same kernel embedded in Rust |
| Host tools and hooks are Python callables | A remote client's tools and callbacks are `tool.call` and `hook.call` requests, each with a timeout and, for a callback, a fail mode |
| The host process is trusted | stdio is trusted; the socket and HTTP act for a tenant, with tokens, allowlists and caps |
| Each agent connects its own store | Agents on one Postgres DSN share one pool across a server's connections |
| `reload_skills()` and `reload_subagents()` on the agent | Not RPC methods at this revision; the core and facade have them ([reload](./skills-subagents.md#catalog-and-reload)) |

## Tests

| Test | Pins |
|---|---|
| `crates/linch-rpc/tests/wire.rs` | Framing and strictness, every JSON-RPC error on the raw wire |
| `crates/linch-rpc/tests/runs.rs`, `approve.rs`, `cancel.rs`, `sessions.rs`, `close.rs`, `skipped.rs` | The methods end to end |
| `crates/linch-rpc/tests/host_tools.rs`, `callbacks.rs`, `permissions.rs`, `requests.rs`, `mcp.rs` | Server requests, their timeouts and cancels, MCP servers |
| `crates/linch-rpc/tests/login.rs`, `tenants.rs`, `allowlists.rs`, `limits.rs`, `client_limits.rs` | Login, admission, every cap and the backpressure on both sides |
| `crates/linch-rpc/tests/pools.rs`, `shutdown.rs` | Shared Postgres pools; the end of input |
| `crates/linch-server/tests/stdio.rs`, `socket.rs`, `http.rs`, `listen.rs` | The binary and its transports |
| `python/tests/test_protocol.py`, `python/tests/test_config.py` | The Python client's wire reading, error map and config fields |

---

Back to the [architecture index](./README.md).

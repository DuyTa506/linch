# Running the server for remote clients

Use `linch-server --config` when clients in other processes or on other
machines should share one sidecar, each tenant with its own tokens,
allowlists and limits.

[← Usage guide](./README.md)

By default `Server()` starts `linch-server` as a child process and speaks to
it over stdin and stdout. That client owns the server, so there is no login,
no tenant and no allowlist. `linch-server --config FILE` instead listens on a
Unix socket, on HTTP with WebSocket, or both, for many clients at once. Each
connection logs in with a bearer token and acts for that token's tenant, and
every `agent.open` passes the tenant's allowlists before anything runs.

From Python, `Server.connect(address, token=...)` reaches such a server and is
used like `Server()` ([below](#connect-from-python)). Clients in other
languages speak the JSON-RPC protocol documented in the `linch-rpc` crate docs
(`crates/linch-rpc/src/lib.rs`); Rust programs can use `linch_rpc::Client`.

| | `linch-server` (stdio) | `linch-server --config FILE` |
|---|---|---|
| Started by | `Server()` in your process | You, as a service |
| Clients | The one that started it | Many: `Server.connect(...)`, over a socket or WebSocket |
| Login | None | `connection.auth` or `Authorization: Bearer` |
| Allowlists and limits | None | Per tenant |
| Stops | When its stdin closes | On SIGINT or SIGTERM: live runs are dropped, a worker's jobs get `worker.drain_timeout_ms` to finish ([Scheduler](./scheduler.md#stopping-a-worker)) |

`linch-server --migrate` and `linch-server --check` are the third use of the
binary: they apply or read the database schema and exit
([Stores: Migrations as a deploy step](./stores.md#migrations-as-a-deploy-step)).

---

## Start a listening server

`/etc/linch/linch-server.json`:

```json
{
  "socket": "/run/linch/linch.sock",
  "http": "127.0.0.1:8731",
  "tenants": {
    "acme": {
      "tokens": [{"env": "LINCH_TOKEN_ACME"}],
      "allow": {
        "scripted_provider": true,
        "permission_modes": ["default", "accept_edits"],
        "workspace_roots": ["/srv/linch/acme"],
        "max_runs": 4,
        "max_connections": 2
      }
    },
    "ops": {
      "tokens": [{"env": "LINCH_TOKEN_OPS"}],
      "allow": {
        "exec_tools": true,
        "permission_modes": ["default", "accept_edits", "skip_dangerous"],
        "workspace_roots": ["/srv/linch/ops"],
        "provider_base_urls": ["http://vllm.internal:8000/v1"],
        "postgres_dsns": ["postgres://linch_ops@db.internal:5432/linch"],
        "mcp_commands": [
          {"command": "/usr/local/bin/git-mcp", "args": ["--read-only"], "env": ["GIT_TOKEN"]}
        ],
        "mcp_urls": ["https://mcp.internal"]
      }
    }
  }
}
```

```bash
export DEEPSEEK_API_KEY=...          # provider keys the agents may use
export LINCH_TOKEN_ACME=... LINCH_TOKEN_OPS=...
cd /var/lib/linch                    # a directory without a .linch folder
linch-server --config /etc/linch/linch-server.json
```

```text
linch-server: listening on unix:/run/linch/linch.sock
linch-server: listening on ws://127.0.0.1:8731/rpc
```

- The config is read once at startup, and strictly: an unknown field at any
  level, an unset token variable, a workspace root that does not exist or a
  cap of 0 stops the server with exit status 2 and the reason on stderr.
  Exit status 1 means an I/O failure; 0 a clean stop.
- A socket file left by a stopped server is replaced, and the file is
  removed at shutdown. Its permissions follow the umask.
- HTTP is plain: put TLS in front of it, or listen on loopback only.
- Run the server from a directory without `.linch`. A tenant's agent with
  skills or subagents on and no workspace resolves the default `.linch`
  against the server's working directory, and is refused when that exists
  outside the tenant's roots.

---

## Config

| Field | Meaning |
|---|---|
| `socket` | Path of a Unix socket to listen on |
| `http` | Address for HTTP with WebSocket, such as `127.0.0.1:8731` |
| `tenants` | Each tenant by name: 1 to 64 ASCII letters, digits, `.`, `_` or `-` |
| `tenants.<name>.tokens` | Its bearer tokens, each `{"env": "VAR"}` (read at startup) or `{"value": "..."}` |
| `tenants.<name>.allow` | Its allowlists, below |
| `cache_salt_secret` | `{"env": "VAR"}` or `{"value": "..."}`, at least 16 bytes: what the tenants' prompt-cache salts are derived from ([below](#prompt-cache-isolation)). Required by a tenant whose `allow.cache_isolation` is on |
| `worker` | Run a worker in this process: it fires schedules and runs background jobs ([Scheduler](./scheduler.md#run-a-worker)). Absent: none |
| `liveness` | How the HTTP listener notices a client that vanished: TCP keepalive and WebSocket pings ([below](#liveness)). Every field has a default |

At least one of `socket`, `http` and `worker` is required, and with a
listener at least one token. A token names exactly one tenant: the same
token twice is refused. A tenant only a worker serves needs no token.

| `allow` field | When absent | What it allows |
|---|---|---|
| `scripted_provider` | `false` | The `scripted` provider, for tests and evals |
| `provider_info` | `false` | `provider.capabilities`, `provider.models` and `provider.model`: the model calls send the server's key to the provider's `base_url`, which must be allowed too. `provider.models` and `provider.model` are limited to 10 a minute per tenant (`-32005`, `kind: provider_calls`); `provider.capabilities` sends no key |
| `exec_tools` | `false` | `Bash` for workspace agents, which runs commands through the agent's execution backend |
| `task_ledger` | `false` | A config's `task_ledger`: one task list shared by all the tenant's sessions, whose open tasks every request carries ([Tasks](./tasks.md#a-ledger-that-outlives-sessions)) |
| `cache_isolation` | `false` | Keeps the tenant's provider prompt cache apart from other tenants' on the same key: every agent sends a salt derived from the tenant's name (`cache_salt` to `vllm` and `sglang`, `prompt_cache_key` to `openai-chat` and `openai-responses`), and an agent on a provider with no such field is refused. It needs the config's top-level `cache_salt_secret` ([below](#tenants-and-namespaces)) |
| `provider_request` | `false` | A provider's `request` (body fields, headers, a timeout) and `chat` settings ([Providers](./providers.md#request-settings)). Credential, `proxy-*` and forwarding headers stay refused, and the counts and sizes are bounded |
| `max_runs` | 16 | Runs the tenant drives at once, across all its connections |
| `max_agents` | 16 | Agents the tenant keeps open at once, across all its connections |
| `max_connections` | 8 | Connections acting for the tenant at once |
| `max_pool_connections` | The server's default pool size: the number of cores, between 10 and 32 | The largest `stores.max_connections` a config may ask for; the tenant's pool on each target has at most this many connections |
| `max_provider_concurrency` | none: no cap | The most provider calls the tenant's runs make at once on each provider key and endpoint; a config may ask for less ([Providers](./providers.md#limit-concurrent-calls)) |
| `provider_base_urls` | none | Endpoints a provider `base_url` may name, matched on scheme, host, port and path. An entry with a path (`https://gw.example/openai/v1`) allows that path alone; one with none (`https://gw.example`) allows each provider's own path there (`/v1` for OpenAI, none for DeepSeek, Anthropic and Gemini, `/v1` or none for vLLM, SGLang and llama.cpp). A `base_url` with a query, fragment, userinfo, escape, backslash or a dot or empty path segment is refused whatever is listed |
| `permission_modes` | `default`, `accept_edits` | The modes a config may choose |
| `workspace_roots` | none | Directories a `workspace`, a `project_root` or a `config_dir` may be or lie under |
| `postgres_dsns` | none | Postgres targets a store DSN may use: host, port or socket, database, user, startup options and TLS files must all match one entry, and its `sslmode` must be the entry's or stricter |
| `mcp_commands` | none | MCP stdio servers, `{"command", "args", "env"}`: the command and argument list must match exactly, and the transport may set only the `env` names listed |
| `mcp_urls` | none | Endpoints an MCP HTTP transport may name, matched like `provider_base_urls` |
| `max_schedules` | none: no schedules | Schedules the tenant may keep in each database ([Scheduler](./scheduler.md#what-a-tenant-may-schedule)); without it an agent with `schedule_tools` is refused |
| `min_schedule_interval_ms` | 60000 | The shortest time between two fires of its schedules |
| `tool_cache` | `false` | `spec.tool_cache`, which keeps results in the server's memory or database for the tenant's namespace; bounded to 1024 entries, a one-hour ttl, 1 MiB a result and 32 tools. The agent also needs `spec.redaction` over tool results, and the entries serve only the session that stored them |
| `tool_cache_shared` | `false` | Whether the tenant's `tool_cache` may use `scope: "namespace"`, which serves one session's result to every session of the namespace; a read tool may answer per session, so the operator decides |
| `max_tool_cache_bytes` | 67108864 (64 MiB) | Bytes of cached results all of the tenant's namespaces may hold together; at least 1 |
| `workflow` | none: no workflows | `workflow.*` ([Workflows](./workflows.md#admission)): `{}` allows them with the default caps, or set `max_workflows` (4), `max_records` (10000), `max_fan_out` (8), `max_record_bytes` (1 MiB), `max_tokens`, `max_cost_usd` |
| `max_queued_jobs` | none: no background runs | Unfinished background jobs it may have when it queues one |
| `system_sections` | none | System prompt sections `{"name", "text", "order"?, "cacheable"?}` added to every agent of the tenant; the tenant cannot remove or rename them, and `replace_defaults` leaves them in. Changing them refuses the resume of the tenant's in-flight runs ([Agent config](./agent.md#system-sections)) |
| `memory` | `false`: no long-term memory | Whether a config may turn on [`memory`](./memory.md#built-in-long-term-memory) |
| `max_memories` | none, which refuses `memory` (the server will not load a config with `memory` and no cap) | Memories it may keep in all its namespaces of a database; an agent's own `max_memories` and `max_memories_per_scope` are held to it |
| `required_policy` | none | A [`spec.policy`](./agent.md#policy) every agent of the tenant carries: the tenant's own policy is added to it, and the operator's rules stay in the run. It orders the model's calls, counted from the agent's own tool results; it does not bind what the tenant's host does (a host tool, hook or cache can answer a required call), so keep permissions and the sandbox for security ([below](#what-admission-refuses-and-why)) |
| `execution` | `[{"backend": "local"}]` | The execution backends a config may choose, each with its whole policy; a config's `execution` must equal one, and a config naming none gets the first, so `[{"backend": "sandbox"}]` holds the tenant to the sandbox ([Sandbox](./sandbox.md)) |

The live providers at their public endpoints and the memory stores need no
entry. A `postgres_dsns` entry without a user stands for the server's own
user; one without options or TLS files allows none. Its `sslmode` is the
weakest a DSN may ask for, in the order `disable`, `allow`, `prefer`,
`require`, `verify-ca`, `verify-full`: an entry with `sslmode=verify-full`
refuses a DSN with `sslmode=require` or none. An entry or DSN without
`sslmode` has the server's default, `PGSSLMODE` or else `prefer`, so a DSN
with `sslmode=disable` needs an entry that says so. An `mcp_commands` entry
may not list `PATH`, `HOME`, `XDG_CONFIG_HOME`, `BASH_ENV`, `ENV`, `LD_*`,
`DYLD_*`, `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `PYTHONUSERBASE`,
`NODE_OPTIONS`, `NODE_PATH`, `npm_config_*` (in any case), `PIP_*`, `UV_*`,
`PERL5LIB`, `PERLLIB`, `PERL5OPT`, `RUBYLIB`, `RUBYOPT`, `GEM_HOME`,
`GEM_PATH`, `CLASSPATH`, `JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS`,
`JDK_JAVA_OPTIONS` or `GCONV_PATH`: they decide what code a program runs, or
which rc and config files it reads (an `.npmrc` under `HOME` can set
`node-options` for `npx`). That list is best effort, not a guarantee: a
program may read other variables, so list only names whose effect on that
program you know. A bare command name is looked up on the server's `PATH`;
prefer absolute paths.

---

## Connect from Python

`Server.connect(address, token=...)` takes the address the server printed at
startup: `unix:/run/linch/linch.sock` for the socket, `ws://127.0.0.1:8731/rpc`
for the WebSocket (`wss://` when TLS is in front of it). Use it as an async
context manager, like `Server()`: entering logs in, and leaving closes the
connection, which drops the runs it drives where they stand (a Postgres run
stays resumable). Host tools, callbacks, approvals and the event stream work
as over stdio. The WebSocket needs the `websocket` extra, which adds
`websockets`; the socket needs nothing more:

```bash
pip install 'linch-rust[websocket]'
```

```python
import asyncio
import os

from linch_rust import AgentConfig, Allow, NotAllowed, Server, SuspendedEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn

SOCKET = os.environ.get("LINCH_SOCKET", "unix:/run/linch/linch.sock")
WEBSOCKET = os.environ.get("LINCH_WEBSOCKET", "ws://127.0.0.1:8731/rpc")
TOKEN = os.environ["LINCH_TOKEN_ACME"]


@tool(scope="write")
async def save_note(text: str) -> str:
    """Save a note for the support team."""
    return f"saved: {text}"


def support(turns: list[dict]) -> AgentConfig:
    return {
        "namespace": "acme/support",
        "spec": {"model": MODEL, "max_turns": 20},
        "provider": {"id": "scripted", "turns": turns},
    }


async def main() -> None:
    async with Server.connect(SOCKET, token=TOKEN) as server:
        try:
            await server.open_agent({**support([]), "tasks": True})
        except NotAllowed as refused:
            print("refused:", refused.kind)

        turns = [tool_turn("c1", "save_note", {"text": "call back"}), text_turn("Saved.")]
        agent = await server.open_agent(support(turns), tools=[save_note])
        run = await agent.run("s1", "Note: call back")
        stopped = await run.result()
        assert isinstance(stopped, SuspendedEvent)
        print("asks:", stopped.pending_request_ids)
        result = await (await agent.approve(run.id, {"c1": Allow()})).result()
        print("final:", result.final_text)

    async with Server.connect(WEBSOCKET, token=TOKEN) as server:
        agent = await server.open_agent(support([text_turn("Hello over WebSocket.")]))
        print("final:", (await (await agent.run("s2", "hi")).result()).final_text)


asyncio.run(main())
```

```text
refused: auto_allow
asks: ['c1']
final: Saved.
final: Hello over WebSocket.
```

| Raised | When |
|---|---|
| `ValueError` by `Server.connect` | The address is not `unix:`, `ws://` or `wss://` |
| `OSError` on entering | Nothing listens at the address |
| `Unauthenticated` (-32003) on entering | The token is not in the config (an HTTP `401` on the WebSocket) |
| `LimitReached` (-32005) on entering | The tenant holds `max_connections` connections (`kind == "connections"`; an HTTP `429` on the WebSocket), or 32 logins wait at once (`kind == "logins"`) |
| `IncompatibleServer` on entering | The [version handshake](#the-version-handshake) failed: the server's protocol is outside the range this client speaks, or the server predates `server.info` |
| `HandshakeTimeout` on entering | The server did not answer `server.info` within 15 seconds |
| `NotAllowed` (-32004) | `open_agent` or a run call failed a [check](#what-admission-refuses-and-why); `.kind` names it |
| `LimitReached` (-32005) by `open_agent` | The tenant keeps `max_agents` agents open; nothing started (`kind == "agents"`) |
| `LimitReached` (-32005) by `run`, `resume` or `approve` | The tenant drives `max_runs` runs; nothing started (`kind == "runs"`, `.max` the cap) |

- A remote server owns the provider keys: they come from its environment, and
  `Server.connect` takes no `env`. `server.pid` raises `LinchError`.
- `server.tenant` is the tenant a remote connection acts for (`"acme"`), as
  the socket's login answer or the WebSocket upgrade's `Linch-Tenant` header
  names it. It is `None` for a server spawned with `Server()`.
- Each connection has its own agent handles (`agent-1` is always the
  connection's own) and can cancel only the runs it drives. A Postgres run
  can be resumed or approved from another connection that opens the same
  config.

### The version handshake

Entering `Server()` or `Server.connect(...)` makes one more call before it
returns: `server.info`, after the login. The answer is on `server.info`:

```python
async with Server.connect(SOCKET, token=TOKEN) as server:
    info = server.info
    info.server_version      # "0.1.1"
    info.protocol            # 1: the wire protocol; this client speaks 1 to 1
    info.contract_version    # 1: the run contract digest's version
    info.has("cache_isolation")   # an optional feature, by name
    info.has("exec_idempotency_key")   # exec.start carries the call's key
    info.backends            # ("local", "sandbox", "docker", "remote")
    info.max_message_bytes   # 16777216
    info.default_max_runs    # 16
```

- **A mismatch fails at once, by name.** A protocol outside the client's range
  raises `IncompatibleServer` (`.protocol`, `.server_version`, `.supported`),
  and so does a server that has no `server.info` (`.protocol` is `None`): a
  binary older than the client. The child process is stopped, or the
  connection closed, before the error reaches you, so no tenant connection
  place leaks. A server that never answers raises `HandshakeTimeout`.
- **The protocol number moves rarely.** It changes only for a wire change an
  older client would misread. A new method, member or optional feature adds a
  capability name instead; check it with `info.has(...)` and ignore names you
  do not know.
- **Every connection gets the same answer.** A tenant sees what a local
  client sees: constants of the build, none of its own caps, names, hosts or
  paths. `backends` lists what the build implements, not what the host can run
  or the tenant may use; `default_max_runs` is the default cap, not the
  tenant's own.

Over stdio on Linux, `Server()` enlarges the child's stdout pipe from 64 KiB to
256 KiB right after the spawn (`F_SETPIPE_SZ`). The server joins queued events
into lines of up to 64 KiB, so with the default pipe a burst of events made it
block on almost every write. If the kernel refuses the resize (for example over
`fs.pipe-user-pages-soft`), the pipe keeps its default size, a debug log line
says so, and the start goes on. Nothing changes on the wire, and the Unix
socket and WebSocket transports are not affected.

---

## The protocol on each transport

- **Socket:** one JSON-RPC 2.0 message per line, as on stdio. The first line
  must be `connection.auth` with a token, answered with `{"tenant": "acme"}`;
  after that the connection takes the same methods as stdio: `agent.open`
  with an [agent config](./agent.md), `run.start`, `run.resume`,
  `run.approve`, `run.cancel`, `run.events`, `eval.run`, `run.report`,
  `session.close`, `agent.close`.
  A first message other than `connection.auth`, or an unknown token, gets
  `-32003` and the connection closes; so does 10 seconds of silence.
- **HTTP:** `GET /rpc` with `Authorization: Bearer <token>` upgrades to a
  WebSocket that carries one JSON-RPC message per text frame, with the same
  methods and no `connection.auth`. Its `101` response names the token's
  tenant in a `Linch-Tenant` header, as the socket's login answer does.
- On both, run events arrive as `run.event` notifications and end with
  `run.end`, and the server sends `tool.call` and `hook.call` requests on the
  same connection for the client to answer.

The HTTP server answers before the upgrade:

| Request | Answer |
|---|---|
| No token, or an unknown one | `401` with `WWW-Authenticate: Bearer` |
| The tenant already holds `max_connections` | `429`, the body naming the cap |
| A valid token without WebSocket headers | `400` |
| A valid WebSocket upgrade | `101 Switching Protocols`, with `Linch-Tenant: <name>` naming the token's tenant |
| A request head not complete within 10 seconds | No answer: the connection closes |

```bash
curl -i http://127.0.0.1:8731/rpc                     # HTTP/1.1 401 Unauthorized
curl -i -H "Authorization: Bearer $LINCH_TOKEN_ACME" \
     http://127.0.0.1:8731/rpc                        # HTTP/1.1 400 Bad Request
```

---

## Tenants and namespaces

- An agent's `namespace` must be the tenant's name or start with
  `<tenant>/` (`acme`, `acme/support`). Stores are keyed by namespace, so on
  a shared Postgres database another tenant's runs and sessions look missing
  (`-32021`).
- A `principal` passed to `run.start`, `run.resume` or `run.approve`
  (`agent.run(..., principal=...)` from Python) must name the connection's
  tenant, or the call raises `NotAllowed` (`kind == "principal"`). The server
  does not fill one in.
- Tokens are kept only as SHA-256 digests and compared in constant time. No
  token is written to any output.

### What keeps tenants apart

Every table the stores and the worker use (`linch_runs`, `linch_run_events`,
`linch_sessions`, `linch_session_entries`, `linch_files`, `linch_memories`,
`linch_mailbox`, `linch_tasks`, `linch_tool_cache`, `linch_ext_workflows`,
`linch_worker_schedules`) has `namespace` in its primary key. The job tables
(`linch_worker_jobs`, `linch_worker_history`) are keyed by one global id and
carry a `namespace` column that `job.get` and `job.list` always bind
(`crates/linch-worker/src/status.rs:153-186`). Each query names the namespace,
so a run, session, file, memory, workflow, job or schedule is found only under
the namespace of the agent that asks. A tenant's
namespaces all start with its name, and the name never holds `/`, so
`acme/support` cannot be reached from `acme-eu`. Run, session, workflow, job
and schedule ids that another tenant holds read exactly like ids nobody has:
the same code, message and data, with the asked id the only difference
(`tenant_oracle` in `crates/linch-rpc/tests` pins it for `run.events`,
`run.report`, `run.resume`, `run.approve`, `run.answer`, `run.steer`,
`run.cancel`, `workflow.status`, `workflow.start`, `job.get`,
`schedule.pause`, `schedule.resume`, `schedule.delete` and `session.close`).
A refused claim (`-32033`) shows only the owner of a run in the tenant's own namespace.

What the key does not do, and what to do about it:

- **One database, one set of tables.** Isolation is the namespace in each
  query, not row-level security or a schema per tenant. A tenant that must
  survive a bug in a query gets its own database or schema: give it its own
  `postgres_dsns` entry (a DSN with its own `search_path`).
- **Users of one tenant are not apart.** A run, session, file and the shared
  memory scope belong to the namespace. `principal.user` scopes only the
  per-principal memory and the permission roles. To keep two users from each
  other, give each its own namespace (`acme/ada`, `acme/bob`).
- **Queue names are shared.** `job.enqueue` and `schedule.create` take a
  `queue` the tenant chooses, and `linch_worker_queues` (the `max_held`
  concurrency limits) is keyed by the name alone. A tenant that names another
  tenant's queue lands in its FIFO and counts against its limit. Name one queue
  per tenant in the worker's `queues` (the worker serves its queues in turn, so
  that is a fair share) and have the host set the `queue` it sends.
- **Job ids are one counter.** `linch_worker_jobs.id` is a single identity
  sequence, so a tenant that reads its own job ids learns how many jobs the
  server queued in between. It reads nothing else.
- **A worker's queue is first in, first out.** A tenant with a large
  `max_queued_jobs` can fill the worker's `max_jobs` slots and make another
  tenant's jobs of the same queue wait. The per-tenant `max_runs` bounds what
  it drives at once, not what it holds in the queue; set `max_queued_jobs`
  to what the worker can drain.
- **A provider key is shared.** `max_provider_concurrency` caps one tenant's
  calls on one key and endpoint, and each tenant has its own gate, so no
  tenant holds another's places. There is no cap across tenants: the sum of
  the tenants' caps is what the vendor sees on the one key, so keep that sum
  under the vendor's limit. The vendor's prompt cache is shared too, unless
  `cache_isolation` is on.

#### Prompt-cache isolation

With the config's `cache_salt_secret` and `allow.cache_isolation` on for a
tenant, admission puts a salt into every agent of that tenant: the lower-case
hex HMAC-SHA256 of the tenant's name under the secret (64 characters, 256
bits), in the field the provider has for it: `cache_salt` on `vllm`, `sglang` and
`prompt_cache_key` on `openai-chat` and `openai-responses`. The same tenant
sends the same salt on every run, from every server and worker that has the
same secret, so its own cache hits are what they were; two tenants send
different salts. The salt is not the tenant's name, it is not in the config
(a stored schedule or job holds none; the worker derives it again), a tenant's
`extra_body` cannot replace it, and it is hidden from `Debug` and from an
error a provider echoes. Keep the secret at least 16 bytes, the same on every
server and worker of the fleet, and out of the repository; changing it gives
every tenant a new salt, which starts their caches cold. See
[Providers](./providers.md#keep-one-tenants-prompt-cache-from-anothers) for
what each vendor does and does not offer.

```json
{
  "cache_salt_secret": {"env": "LINCH_CACHE_SALT_SECRET"},
  "tenants": {"acme": {"tokens": [{"env": "ACME_TOKEN"}],
                       "allow": {"cache_isolation": true,
                                 "provider_base_urls": ["https://vllm.internal/v1"]}}}
}
```

---

## Limits

| Limit | Value | Past it |
|---|---|---|
| `max_runs` | 16 per tenant unless set | `run.start`, `run.resume` or `run.approve` gets `-32005`, `data: {"kind": "runs", "max": ...}`; nothing starts. A run holds its place until its `run.end`; one that suspends with background subagents still running keeps it until they end, or until its approve or resume takes it over |
| `max_agents` | 16 per tenant unless set | `agent.open` gets `-32005` (`kind: agents`) after its checks pass, before any MCP server starts. An agent holds its place until `agent.close`, or the end of its connection, has stopped its MCP servers |
| `max_connections` | 8 per tenant unless set | The socket login gets `-32005` (`kind: connections`) and closes; HTTP gets `429` |
| Logins waiting at once | 32, across the server | `-32005` (`kind: logins`) and the connection closes |
| Login wait | 10 seconds | `-32003` and the connection closes |
| HTTP connections waiting for their upgrade | 32, across the server; an upgraded one gives its place back | The connection is closed unread |
| HTTP request head | 10 seconds for each head before the upgrade | The connection closes without an answer |
| A vanished HTTP client | TCP keepalive (30 s idle, 3 probes 10 s apart) and a TCP user timeout of 60 s; a WebSocket ping every 30 s and 60 s of silence ([Liveness](#liveness)) | The connection ends, freeing its `max_connections` place, its workflow holds and its calls |
| One message | 16 MiB | The request gets `-32600` |
| A Postgres pool | One per tenant and target, `stores.max_connections` connections (the number of cores, between 10 and 32, unless set) | Queries wait for a connection; the tenant's open that connects its pool sets the size and its later opens share it ([Stores](./stores.md#connections-and-pools)) |
| `max_pool_connections` | The default pool size per tenant unless set | `agent.open` asking for a larger `stores.max_connections` gets `-32004`, `data: {"kind": "pool_connections", "max": ...}`; nothing connects |
| `max_provider_concurrency` | No cap unless set | `agent.open` asking for more gets `-32004`, `data: {"kind": "provider_concurrency", "max": ...}`; one that asks for none gets the cap. All the tenant's agents on one provider key and endpoint share that many provider calls; a worker process or a second server counts its own |
| `max_tool_cache_bytes` | 64 MiB per tenant unless set | Admission writes `{"group": <tenant>, "max_bytes": ...}` into the agent's `tool_cache.quota`, whatever the client sent, and every put makes the store drop the tenant's oldest entries (in Postgres by a running sum of the stored lengths over the tenant's rows; in memory over the agent's own store) until the total fits. A memory-store agent holds its own store, so a tenant's memory is at most this cap times its open agents |
| Requests in flight per connection | 64 | Reading waits until one is answered |
| Background subagents per run | The agent config's `workers.max_background`, 4 unless set | The `Subagent` call gets a tool error and starts nothing ([Subagents](./subagents.md#background-workers)) |

The defaults bound what one tenant spends: each run streams from a provider
on the server's key, each open agent holds its config in memory and runs its
MCP stdio servers as child processes, and each connection may buffer a large
message. A run's background subagents stream too, outside the run's place,
up to its `workers.max_background` (4 unless set) at once, so one tenant's
running runs have at most `max_runs × (1 + max_background)` provider streams
open: 80 with the defaults. Read-only foreground subagents that run in
parallel count up to the spec's `max_tool_concurrency` in place of the
parent's one. A run awaiting approval keeps its background subagents
running until it is approved or resumed, or its session or agent closes,
and keeps its one place while any of them runs, so the bound holds.

---

## What admission refuses, and why

Every check runs before anything happens: for a refused `agent.open` no key
is read, no MCP process starts and no database is connected. A refusal is
`-32004` with `data.kind` naming the check: `NotAllowed` in Python, whose
`kind` is that name.

| `kind` | Refused when | Why |
|---|---|---|
| `namespace` | The namespace is not the tenant's | Stores are scoped by namespace |
| `principal` | `principal.tenant` is another tenant | A client cannot act for another tenant |
| `provider` | The `scripted` provider without `scripted_provider` | It is for tests and evals |
| `cache_isolation` | `allow.cache_isolation` is on and the agent's provider has no request field for a cache salt (`deepseek`, `anthropic`, `gemini`, `llamacpp`) | The operator asked for an isolated cache and the vendor offers no way to give one; see [Providers](./providers.md#keep-one-tenants-prompt-cache-from-anothers) |
| `provider_base_url` | A `base_url` outside `provider_base_urls` (origin or path), or one that is not plain (query, fragment, userinfo, escape, dot segments); vLLM, SGLang and llama.cpp always name one | The server's key would be sent there, or to another path of that origin |
| `permission_mode` | A mode outside `permission_modes` | The mode decides what runs without asking |
| `auto_allow` | `auto_allow` entries, `tasks: True` or a `deep_agent`, without `skip_dangerous` in `permission_modes` | They let calls run without asking, as `skip_dangerous` does; `tasks` and `deep_agent` add the four task tools to `auto_allow` |
| `permission_rule` | A rule that allows, without `skip_dangerous` | The same; rules that ask or deny always pass |
| `workspace` | Not an existing directory under `workspace_roots` | The workspace tools read and write there |
| `project_root` | The same, for `permissions.project_root` | Path rules resolve against it; with no roots, a config cannot send `permissions` at all and keeps the default policy |
| `config_dir` | With skills or subagents on: not an existing directory under the roots, through an absolute path, `..` or a symlink | `SKILL.md` and agent files would load from another tenant's tree |
| `postgres` | A DSN that matches no `postgres_dsns` entry exactly, or asks for a weaker `sslmode` | A client could pick another database, role, `search_path` or certificate file, or send the server's credentials without TLS |
| `pool_connections` | `stores.max_connections` above the tenant's `max_pool_connections` | Every pool on a database shares its connection limit; `data.max` is the cap |
| `mcp_command` | A stdio command and argument list that match no entry | The server would start that program |
| `mcp_env` | An `env` name the matching entry does not list | It could change what the program loads |
| `mcp_url` | An MCP HTTP URL outside `mcp_urls` | The server would connect there |
| `execution` | An `execution` that equals no `allow.execution` entry, or none when that list is empty | The backend decides what `Bash` can reach; its policy is the operator's |
| `redaction` | More than 32 `spec.redaction` rules, or a pattern over 1024 bytes | The server compiles each pattern and runs it on every response and tool result |
| `verification` | More than 32 `spec.verification` verifiers, a pattern over 1024 bytes, or more than 32 keywords in one | The same, for every final answer |
| `provider_concurrency` | `max_provider_concurrency` above the tenant's | The tenant's runs would make more calls at once on the server's key than the operator allows; `data.max` is the cap |
| `system` | More than 16 `spec.system` sections, a name over 64 bytes, a section or `append` over 16 KiB, or over 64 KiB together | Sections ride every request of every run |
| `context_budget` | More than 32 ids in `drop_order` or `pinned`, or an id over 128 bytes | The server keeps them per agent |
| `tool_cache` | `spec.tool_cache` without `allow.tool_cache`; without `spec.redaction` over tool results; with `scope: "namespace"` and no `allow.tool_cache_shared`; or past its bounds (`max_entries` 1024, `ttl_ms` 3600000, `max_value_bytes` 1048576, 32 tools) | Entries are stored by the server, so what a tool returned must be redacted first, and a result is served to another session only when the operator allows. The table or memory they take is the operator's |
| `tool_retry` | `spec.tool_retry.max_attempts` above 5, `max_delay_ms` above 60000, or a list over 32 names | Every retry is a durable write and a wait the run holds a slot through. There is no `allow.tool_retry`: a retry only repeats a tool the agent could already call, so the caps are the whole control |
| `policy` | More than 32 prerequisites or gates, more than 32 tools or keywords in one, a text over 1024 bytes, `max_corrections` above 16, or a gate that redefines one of `required_policy`'s | The server checks each rule on every tool batch and final answer, and each correction is a provider call the policy asks for; the operator's gates are not the tenant's to change |
| `loop_guard` | `spec.loop_guard.max_idle_turns` above 25 | Each turn without a provider call is a write and a session append on the server that no budget counts |
| `run_bound` | `spec.max_turns` is unset, the `spec.budget` sets neither `max_tokens` nor `max_cost_usd`, and neither the `profile` nor the `deep_agent` profile supplies `max_turns` | A tenant's run must have some bound: a model that keeps calling a tool would run on the operator's key until the loop guard, which an input that keeps changing evades (see [loop guard](./agent.md#loop_guard)). The refusal text names both settings. Nothing is added to the spec, so the run contract is unchanged; a trusted `local_namespaces` root and a stdio client are not held to it |
| `tool_choice` | A forced `tool_choice` (`required`, or a tool that is not the final tool), in the spec or in `run.start`, on an agent with no `max_turns` and no `final_tool_name` (an output schema alone does not end the run on every provider) | A model forced to call a tool every turn never answers, and no budget ends the run |

A path that does not exist is refused like one outside the roots, so a
client cannot probe the server's file system. Seven changes are made
silently instead:

- Without `exec_tools`, the config's `bash` is turned off and the agent opens
  without `Bash`. The run's `init` event lists the tools it really has, and
  its contract differs from the same config on stdio.
- The workspace, the project root and the config directory are replaced by
  the canonical paths that were checked.
- A config without `execution` gets the tenant's first `allow.execution`
  entry, which holds the tenant to a sandbox when that is the first.
- A Postgres store without `max_connections` gets the default pool size or
  the tenant's `max_pool_connections`, whichever is smaller.
- A config without `max_provider_concurrency` gets the tenant's, when it has one.
- The operator's `system_sections` are added to the config's own, and a
  section of the config with the same name is replaced by the operator's
  (`allow.system_sections`). They are not counted against the bound
  above, and `replace_defaults` leaves them in.
- With `required_policy`, the config's `spec.policy` is merged with it
  before the agent is built, so it is part of the run contract: a
  prerequisite takes the union of both sides' requirements, the operator's
  gates stand (the tenant may add gates under other names, or repeat one
  as it is), and the corrections allowed are the operator's when the
  tenant sets none, else the lesser of the two caps (a tenant that sets
  `max_corrections` may lower it, not raise it). A rule naming a tool the
  agent lacks refuses the open (`OpenError`), never dropped; the tenant's
  error does not name the tool or echo a pattern, as the operator wrote the rule
  (the server config refuses a `required_policy` whose check does not compile).
  The rules bind the model's call order only: a client's host tool under a
  required name, a `pre_tool_use` hook, a `post_tool_use` replacement or a
  cache hit can still satisfy a prerequisite, so this is not enforcement
  against the tenant's own host.

---

## Many runs on one stdio sidecar

The stdio server runs on one thread. With Postgres stores it spends about 1 ms
of its own CPU per turn at 2.1 GHz, so one sidecar serves about 700 to 750
turns per second. Against the in-process kernel, with a pool of 32 on both,
the gap in run wall time (4 turns, a 1 s provider delay per turn) is 0.8 s at
500 concurrent sessions, 1.3 s at 1000 and 3.4 s at 2000
([Benchmarks](../benchmarks.md)). Above about 500 runs in flight per sidecar:

- Start N sidecars and split `stores.max_connections` between them
  ([Stores](./stores.md#connections-and-pools)), or
- set `LINCH_STDIO_THREADS=N` in the server's environment (N from 2 to 256) to
  run the stdio server on N worker threads. Unset or `1` keeps the one-thread
  runtime. Any other value (not a number, `0`, above 256) stops the server at
  startup with an error naming the variable. The Python client passes it on
  when it is in `Server(env=...)`, or in this process's environment when `env`
  is absent. The variable has no effect on `--config` servers, which already
  use a thread per core.

At 2000 concurrent sessions, 4 turns each, with the same 32 connections: one
thread 9.7 s, 2 workers 9.1 s, 4 workers 7.4 s, 8 workers 6.9 s, 16 workers
6.4 s; 2 sidecars of 16 connections 7.5 s, 4 sidecars of 8 connections 6.3 s.
Threads cost CPU (the sidecar used 7.8 CPU-s on one thread, 13.2 with 4
workers and 17.1 with 16) and 1 to 3 ms per small run, which is why the
default stays one thread; each extra sidecar adds memory and one lease
connection per agent. More pool connections help only once more than one
thread serves them.

## Logging

The server's `tracing` records (the worker's failed jobs, retries and
pruning errors, the scheduler's paused schedules, a workflow journal that
failed, a schedule tool call that failed in the store) go to **stderr**, never
to stdout, which carries the protocol on stdio. By default only warnings and
errors are written. `LINCH_LOG` sets the filter (`RUST_LOG` is read when it
is unset), in the directive syntax of `tracing-subscriber`:

```bash
LINCH_LOG=info linch-server --config server.json            # also skips and retries
LINCH_LOG=linch_worker=debug,warn linch-server --config server.json
```

A value that is not a valid filter is reported on stderr and `warn` is used.
Records can quote what a provider, a tool or the database said, which an
endpoint may have echoed a credential into: keep the log as private as the
database. It carries no token and no key the server read itself. Under
systemd or a container runtime stderr is already collected; with the
Python client's `Server()`, which starts the binary for you, it is the
parent's stderr.

## Which server drives a run

A run on Postgres is driven by one server at a time. A resume, approve or
answer sent to another server is refused with `RunLeased` (-32033), whose
`owner` names the server that holds the run, so an operator can tell which
one to look at. The name is the server's id: 8 random hex characters made at
startup and printed on stderr (`linch-server: server id 3fa91c0b`), or the
`server_id` of the config (1 to 64 ASCII letters, digits, `.`, `_` or `-`).
It is the same for every tenant and connection of that server process and
for the run rows it claims, and it changes at each restart unless it is set.
A tenant sees it in the refusal, so it holds no host name, path or pid: a
configured one should not either. Give each server process its own;
`worker.owner` defaults to it. Over stdio there is one connection per
process and the id is that connection's.

## Liveness

A client that vanishes without closing its connection (a power cut, a dropped
route, a killed VM) would keep its place among the tenant's `max_connections`,
its workflows' holds and its calls in flight until the server restarted. The
HTTP listener therefore:

- sets **TCP keepalive** on every accepted socket and a **TCP user timeout**
  as long as the keepalive takes to give up, so the kernel drops a peer that
  answers neither probes nor data;
- pings each WebSocket every `ping_interval_ms` and ends one that sent
  nothing at all (no frame, no pong) for `pong_timeout_ms`. Silence is judged
  as it grows, not only when a ping goes out: the connection ends
  `pong_timeout_ms` after the last frame the client sent (60 s with the
  defaults), whatever `ping_interval_ms` is. A probe is answered by the peer's
  kernel, so a client that is alive but hung passes it; a ping is answered by
  the client's WebSocket code, so a hung client does not.

Ending a connection is the same as its input ending: its runs are dropped (a
Postgres run keeps its last durable state and can be resumed), its calls are
answered, and its places are freed.

A client that stays connected but **stops reading** (its socket buffers and
the server's queue of 1024 messages fill) pauses its runs, and keeps them: a
run's lease is renewed by a task of its own, so no other server can take the
run over while the connection lives ([Run leases](./stores.md#run-leases)).
A WebSocket that reads nothing cannot take a ping either: it is ended when a
ping cannot be sent within `pong_timeout_ms`, and its runs are dropped as above.
There is no write timeout on a Unix socket or stdio: a local client that never
reads again holds its runs until it closes.

**A client that is busy but alive must keep its event loop running.** The
pong comes from the client's WebSocket code, which runs on its event loop. A
Python client whose loop is blocked for longer than `pong_timeout_ms` (60 s by
default) by a synchronous host tool answers no ping, so the server ends the
connection; the client's next call raises `ConnectionClosed`, and its runs are
dropped as above. A tool that waits (`await`) or runs in a thread
(`asyncio.to_thread`) leaves the loop free and the connection stays. So run a
long blocking host tool in a thread. This applies to the WebSocket only: the
Python client spawns a server over stdio and a Unix socket has no liveness
check, so neither cuts a busy client. `test_remote.py` pins the rule with the
two times shortened (a 400 ms timeout and a client blocked for 1 s).

| `liveness` field | Default | Meaning |
|---|---|---|
| `keepalive_idle_secs` | 30 | Idle time before the first probe, in whole seconds (1 to 32767) |
| `keepalive_interval_secs` | 10 | Time between probes, in whole seconds (1 to 32767) |
| `keepalive_probes` | 3 | Unanswered probes before the connection is dropped (1 to 127), so about 60 s in all |
| `ping_interval_ms` | 30000 | Time between WebSocket pings |
| `pong_timeout_ms` | 60000 | Silence after which a WebSocket is ended; keep it above `ping_interval_ms` |

Each is at least 1, and a keepalive field has the largest value Linux's TCP options take (shown above), so a config the kernel would refuse is refused when it loads. The keepalive times are whole seconds because that is the kernel's unit; there is no millisecond form, and an `_ms` name for them is an unknown field. A **Unix socket** has neither: the kernel closes it when
the peer's process ends, whatever the cause, so it needs no probe. The
Postgres connection that holds a workflow sets the database's own keepalives
([Workflows](./workflows.md#one-driver-per-workflow)).

## What the server's environment protects

The server reads provider keys from its own environment; a config never
carries one.

| Who | What it sees of the server's environment |
|---|---|
| `Bash` | Only `PATH`, `HOME`, `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `USER`, `SHELL`, `TMPDIR`; in the `sandbox` backend `HOME` and `TMPDIR` are its private home, and no other process's `/proc/<pid>/environ` is readable ([Sandbox](./sandbox.md)) |
| An MCP stdio server | Only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`, plus the `env` its transport sets |
| Another process of the same user | On Linux the server is marked not dumpable, so `/proc/<server pid>/environ` and its memory cannot be read |

That protects the server process only. When your Python process starts the
server with `Server()`, keys in the Python process's own environment stay
readable to any process of the same user, the server's children included,
through `/proc/<python pid>/environ`. When agents run untrusted commands,
keep keys out of your process's environment, read them from a secret store,
and hand them to the server alone:

```python
from linch_rust import Server

server = Server(env={"DEEPSEEK_API_KEY": "<read from your secret store>"})
```

`env` is the server's whole environment; nothing else is passed on.

---

## Related pages

- [Agent configuration](./agent.md): what `agent.open` takes
- [Runs](./runs.md): resume, approve, cancel and `run.events`
- [Session tasks](./tasks.md) and [Deep agent](./deep-agent.md): why they
  need `skip_dangerous` here
- [Troubleshooting](./troubleshooting.md)

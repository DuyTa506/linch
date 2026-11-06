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
| Stops | When its stdin closes | On SIGINT or SIGTERM |

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

At least one of `socket` and `http` is required, and at least one token. A
token names exactly one tenant: the same token twice is refused.

| `allow` field | When absent | What it allows |
|---|---|---|
| `scripted_provider` | `false` | The `scripted` provider, for tests and evals |
| `exec_tools` | `false` | `Bash` for workspace agents, which runs commands as the server's user |
| `max_runs` | 16 | Runs the tenant drives at once, across all its connections |
| `max_agents` | 16 | Agents the tenant keeps open at once, across all its connections |
| `max_connections` | 8 | Connections acting for the tenant at once |
| `provider_base_urls` | none | Endpoints a provider `base_url` may name, matched on scheme, host and port |
| `permission_modes` | `default`, `accept_edits` | The modes a config may choose |
| `workspace_roots` | none | Directories a `workspace`, a `project_root` or a `config_dir` may be or lie under |
| `postgres_dsns` | none | Postgres targets a store DSN may use: host, port or socket, database, user, startup options and TLS files must all match one entry |
| `mcp_commands` | none | MCP stdio servers, `{"command", "args", "env"}`: the command and argument list must match exactly, and the transport may set only the `env` names listed |
| `mcp_urls` | none | Endpoints an MCP HTTP transport may name, matched like `provider_base_urls` |

The live providers at their public endpoints and the memory stores need no
entry. A `postgres_dsns` entry without a user stands for the server's own
user; one without options or TLS files allows none. An `mcp_commands` entry
may not list `PATH`, `BASH_ENV`, `ENV`, `LD_*`, `DYLD_*`, `PYTHONPATH`,
`PYTHONHOME`, `PYTHONSTARTUP`, `NODE_OPTIONS`, `NODE_PATH`, `PERL5LIB`,
`PERL5OPT`, `RUBYLIB`, `RUBYOPT`, `CLASSPATH` or `JAVA_TOOL_OPTIONS`: they
decide what code a program runs. A bare command name is looked up on the
server's `PATH`; prefer absolute paths.

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
        "spec": {"model": MODEL},
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
| `NotAllowed` (-32004) | `open_agent` or a run call failed a [check](#what-admission-refuses-and-why); `.kind` names it |
| `LimitReached` (-32005) by `open_agent` | The tenant keeps `max_agents` agents open; nothing started (`kind == "agents"`) |
| `LimitReached` (-32005) by `run`, `resume` or `approve` | The tenant drives `max_runs` runs; nothing started (`kind == "runs"`, `.max` the cap) |

- A remote server owns the provider keys: they come from its environment, and
  `Server.connect` takes no `env`. `server.pid` raises `LinchError`.
- `server.tenant` is the tenant a socket connection acts for, as the login
  answer names it (`"acme"`). It is `None` over the WebSocket, whose upgrade
  does not name it, and for a server spawned with `Server()`.
- Each connection has its own agent handles (`agent-1` is always the
  connection's own) and can cancel only the runs it drives. A Postgres run
  can be resumed or approved from another connection that opens the same
  config.

---

## The protocol on each transport

- **Socket:** one JSON-RPC 2.0 message per line, as on stdio. The first line
  must be `connection.auth` with a token, answered with `{"tenant": "acme"}`;
  after that the connection takes the same methods as stdio: `agent.open`
  with an [agent config](./agent.md), `run.start`, `run.resume`,
  `run.approve`, `run.cancel`, `run.events`, `session.close`, `agent.close`.
  A first message other than `connection.auth`, or an unknown token, gets
  `-32003` and the connection closes; so does 10 seconds of silence.
- **HTTP:** `GET /rpc` with `Authorization: Bearer <token>` upgrades to a
  WebSocket that carries one JSON-RPC message per text frame, with the same
  methods and no `connection.auth`.
- On both, run events arrive as `run.event` notifications and end with
  `run.end`, and the server sends `tool.call` and `hook.call` requests on the
  same connection for the client to answer.

The HTTP server answers before the upgrade:

| Request | Answer |
|---|---|
| No token, or an unknown one | `401` with `WWW-Authenticate: Bearer` |
| The tenant already holds `max_connections` | `429`, the body naming the cap |
| A valid token without WebSocket headers | `400` |
| A valid WebSocket upgrade | `101 Switching Protocols` |
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

---

## Limits

| Limit | Value | Past it |
|---|---|---|
| `max_runs` | 16 per tenant unless set | `run.start`, `run.resume` or `run.approve` gets `-32005`, `data: {"kind": "runs", "max": ...}`; nothing starts. A run holds its place until its `run.end` |
| `max_agents` | 16 per tenant unless set | `agent.open` gets `-32005` (`kind: agents`) after its checks pass, before any MCP server starts. An agent holds its place until `agent.close`, or the end of its connection, has stopped its MCP servers |
| `max_connections` | 8 per tenant unless set | The socket login gets `-32005` (`kind: connections`) and closes; HTTP gets `429` |
| Logins waiting at once | 32, across the server | `-32005` (`kind: logins`) and the connection closes |
| Login wait | 10 seconds | `-32003` and the connection closes |
| HTTP connections waiting for their upgrade | 32, across the server; an upgraded one gives its place back | The connection is closed unread |
| HTTP request head | 10 seconds for each head before the upgrade | The connection closes without an answer |
| One message | 16 MiB | The request gets `-32600` |
| A Postgres pool | One per DSN across the server, `stores.max_connections` connections (10 unless set) | Queries wait for a connection; the open that connects a DSN sets its size and later opens share it ([Stores](./stores.md#connections-and-pools)) |
| Requests in flight per connection | 64 | Reading waits until one is answered |

The defaults bound what one tenant spends: each run streams from a provider
on the server's key, each open agent holds its config in memory and runs its
MCP stdio servers as child processes, and each connection may buffer a large
message. A run's background subagents stream too, up to its
`workers.max_background` (4 unless set) at once, so one tenant has at most
`max_runs × (1 + max_background)` provider streams open: 80 with the
defaults.

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
| `provider_base_url` | A `base_url` outside `provider_base_urls`; vLLM, SGLang and llama.cpp always name one | The server's key would be sent there |
| `permission_mode` | A mode outside `permission_modes` | The mode decides what runs without asking |
| `auto_allow` | `auto_allow` entries, `tasks: True` or a `deep_agent`, without `skip_dangerous` in `permission_modes` | They let calls run without asking, as `skip_dangerous` does; `tasks` and `deep_agent` add the four task tools to `auto_allow` |
| `permission_rule` | A rule that allows, without `skip_dangerous` | The same; rules that ask or deny always pass |
| `workspace` | Not an existing directory under `workspace_roots` | The workspace tools read and write there |
| `project_root` | The same, for `permissions.project_root` | Path rules resolve against it; with no roots, a config cannot send `permissions` at all and keeps the default policy |
| `config_dir` | With skills or subagents on: not an existing directory under the roots, through an absolute path, `..` or a symlink | `SKILL.md` and agent files would load from another tenant's tree |
| `postgres` | A DSN that matches no `postgres_dsns` entry exactly | A client could pick another database, role, `search_path` or certificate file |
| `mcp_command` | A stdio command and argument list that match no entry | The server would start that program |
| `mcp_env` | An `env` name the matching entry does not list | It could change what the program loads |
| `mcp_url` | An MCP HTTP URL outside `mcp_urls` | The server would connect there |

A path that does not exist is refused like one outside the roots, so a
client cannot probe the server's file system. Two changes are made
silently instead:

- Without `exec_tools`, the config's `bash` is turned off and the agent opens
  without `Bash`. The run's `init` event lists the tools it really has, and
  its contract differs from the same config on stdio.
- The workspace, the project root and the config directory are replaced by
  the canonical paths that were checked.

---

## What the server's environment protects

The server reads provider keys from its own environment; a config never
carries one.

| Who | What it sees of the server's environment |
|---|---|
| `Bash` | Only `PATH`, `HOME`, `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `USER`, `SHELL`, `TMPDIR` |
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

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

The `linch_rust` client speaks stdio only: it cannot connect to a listening
server yet. A remote client speaks the JSON-RPC protocol itself, as the
socket example below does. The methods are documented in the `linch-rpc`
crate docs (`crates/linch-rpc/src/lib.rs`); Rust programs can use
`linch_rpc::Client`.

| | `linch-server` (stdio) | `linch-server --config FILE` |
|---|---|---|
| Started by | `Server()` in your process | You, as a service |
| Clients | The one that started it | Many, over a socket or WebSocket |
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

## Connect over the socket

One JSON-RPC 2.0 message per line, as on stdio. The first line must be
`connection.auth` with a token; after that the connection takes the same
methods as stdio: `agent.open` with an [agent config](./agent.md),
`run.start`, `run.resume`, `run.approve`, `run.cancel`, `run.events`,
`session.close`, `agent.close`. Run events arrive as `run.event`
notifications and end with `run.end`.

```python
import asyncio
import itertools
import json
import os

SOCKET = os.environ.get("LINCH_SOCKET", "/run/linch/linch.sock")
TOKEN = os.environ["LINCH_TOKEN_ACME"]


class Rpc:
    """One JSON-RPC 2.0 message per line; notifications are kept for later."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.ids = itertools.count(1)
        self.notifications: list[dict] = []

    async def call(self, method: str, params: dict) -> dict:
        id = next(self.ids)
        message = {"jsonrpc": "2.0", "id": id, "method": method, "params": params}
        self.writer.write((json.dumps(message) + "\n").encode())
        await self.writer.drain()
        while True:
            answer = json.loads(await self.reader.readline())
            if answer.get("id") != id:
                self.notifications.append(answer)
            elif "error" in answer:
                raise RuntimeError(answer["error"])
            else:
                return answer["result"]

    async def notification(self) -> dict:
        if self.notifications:
            return self.notifications.pop(0)
        return json.loads(await self.reader.readline())


async def main() -> None:
    reader, writer = await asyncio.open_unix_connection(SOCKET, limit=64 * 1024 * 1024)
    rpc = Rpc(reader, writer)
    print(await rpc.call("connection.auth", {"token": TOKEN}))

    config = {
        "namespace": "acme/support",
        "spec": {"model": "scripted-model"},
        "provider": {"id": "scripted", "turns": [{"events": [
            {"type": "message_start", "model": "scripted-model"},
            {"type": "text_delta", "text": "Hello from the server."},
            {"type": "message_end", "stop_reason": "end_turn"},
        ]}]},
    }
    try:
        await rpc.call("agent.open", {**config, "tasks": True})
    except RuntimeError as refused:
        print("refused:", refused)

    agent = (await rpc.call("agent.open", config))["agent"]
    run = await rpc.call("run.start", {"agent": agent, "session_id": "s1", "prompt": "hi"})
    while True:
        message = await rpc.notification()
        if message["method"] == "run.event":
            for envelope in message["params"]["envelopes"]:
                if envelope["event"]["type"] == "result":
                    print("final:", envelope["event"]["final_text"])
        elif message["method"] == "run.end" and message["params"]["run_id"] == run["run_id"]:
            break
    writer.close()
    await writer.wait_closed()


asyncio.run(main())
```

```text
{'tenant': 'acme'}
refused: {'code': -32004, 'message': "tasks and deep_agent auto-allow the task tools, so they need skip_dangerous in the tenant's permission_modes", 'data': {'kind': 'auto_allow'}}
final: Hello from the server.
```

- A first message other than `connection.auth`, or an unknown token, gets
  `-32003` and the connection closes; so does 10 seconds of silence.
- Host tools and callbacks work as on stdio: the server sends `tool.call` and
  `hook.call` requests on the same connection, and the client answers them.
  This minimal client declares none.
- Each connection has its own agent handles (`agent-1` is always the
  connection's own) and can cancel only the runs it drives. A Postgres run
  can be resumed from another connection that opens the same config.

---

## Connect over HTTP

`GET /rpc` with `Authorization: Bearer <token>` upgrades to a WebSocket that
carries one JSON-RPC message per text frame, with the same methods as the
socket and no `connection.auth`. The server answers before the upgrade:

| Request | Answer |
|---|---|
| No token, or an unknown one | `401` with `WWW-Authenticate: Bearer` |
| The tenant already holds `max_connections` | `429`, the body naming the cap |
| A valid token without WebSocket headers | `400` |
| A valid WebSocket upgrade | `101 Switching Protocols` |

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
- A `principal` passed to `run.start`, `run.resume` or `run.approve` must
  name the connection's tenant. The server does not fill one in.
- Tokens are kept only as SHA-256 digests and compared in constant time. No
  token is written to any output.

---

## Limits

| Limit | Value | Past it |
|---|---|---|
| `max_runs` | 16 per tenant unless set | `run.start`, `run.resume` or `run.approve` gets `-32005`, `data: {"kind": "runs", "max": ...}`; nothing starts. A run holds its place until its `run.end` |
| `max_connections` | 8 per tenant unless set | The socket login gets `-32005` (`kind: connections`) and closes; HTTP gets `429` |
| Logins waiting at once | 32, across the server | `-32005` (`kind: logins`) and the connection closes |
| Login wait | 10 seconds | `-32003` and the connection closes |
| One message | 16 MiB | The request gets `-32600` |
| Requests in flight per connection | 64 | Reading waits until one is answered |

The defaults bound what one tenant spends: each run streams from a provider
on the server's key, and each connection may buffer a large message.

---

## What admission refuses, and why

Every check runs before anything happens: for a refused `agent.open` no key
is read, no MCP process starts and no database is connected. A refusal is
`-32004` with `data.kind` naming the check.

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

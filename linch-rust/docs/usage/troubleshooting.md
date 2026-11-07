# Troubleshooting

Use this page to match an error from `linch_rust` to its cause and fix.

[<- Usage guide](./README.md)

Messages are quoted as the client shows them. Parts in `<...>` vary; a row
marked "fragment" quotes only the fixed part of a longer message. Every
server error is an `RpcError` with `code`, `message` and `data`; the
exception class tells you its family:

| Class | Codes | Raised by |
|---|---|---|
| `ProtocolError` | -32700 to -32600 | A config or declaration the server cannot read |
| `OpenError` | -32010 to -32015 | `open_agent`: the config, provider, stores or an MCP server failed |
| `RunError` and its subclasses | -32020 to -32030 | The end of a run's stream, or `agent.events` |
| `AgentNotFound`, `RunBusy` | -32001, -32002 | The call itself |
| `ConnectionClosed` | none | Any call or stream, when the server went away |
| `LinchError` | none | The base of all of these, and misuse of `Server` |

---

## The server binary

`Server()` finds `linch-server` in this order:

1. the path in `LINCH_SERVER_BIN`, so a build of your own replaces the
   bundled one;
2. the binary installed with the `linch-rust` wheel;
3. in a checkout, the newer of `target/debug/linch-server` and
   `target/release/linch-server` under `CARGO_TARGET_DIR` or the cargo
   workspace's `target/`.

`Server(binary=...)` skips the search.

| Message | Cause | Fix |
|---|---|---|
| ``no linch-server binary: install the linch-rust wheel, set LINCH_SERVER_BIN, or run `cargo build -p linch-server` in <workspace> (looked in <target dir>)`` (`FileNotFoundError`) | No wheel is installed, the variable is unset, and no cargo build exists | Install the wheel, or build the server in the checkout, or set `LINCH_SERVER_BIN` |
| `[Errno 2] No such file or directory: <path>` (`FileNotFoundError`) | `LINCH_SERVER_BIN` or `binary=` names a file that does not exist | Point it at the built binary |
| A field documented here is refused as unknown (`ProtocolError`, -32602) | The server binary is older than the client: an old wheel, or a stale cargo build that the search still prefers | Reinstall the wheel, or rebuild with `cargo build -p linch-server` |

## The `Server` object

| Message | Cause | Fix |
|---|---|---|
| ``the server is not started; use `async with Server()` `` (`LinchError`) | A call on a `Server` that was never started or was closed | Use `async with Server() as server:` and call inside the block |
| `the server is already started` (`LinchError`) | `start()` called twice | Start each `Server` once |
| `the server closed its output` (`ConnectionClosed`) | The server process exited: it was killed, crashed, or the `Server` block ended while a run was streaming | Start a new `Server`. With Postgres stores, resume the run ([Runs](./runs.md#resume)); memory runs are gone |
| fragment: `reading the server failed:` (`ConnectionClosed`) | A server line could not be read or parsed | Report it with the server's stderr |

### A remote server (`Server.connect`)

| Message | Cause | Fix |
|---|---|---|
| `a server address is unix:PATH, ws://HOST:PORT/rpc or wss://...` (`ValueError`) | Another scheme, such as `http://`, or a bare path | Copy the address the server printed at startup |
| `[Errno 2] No such file or directory` or `[Errno 111] Connect call failed` (`OSError`) | Nothing listens there | Start `linch-server --config`, or fix the path or port |
| `authentication required: the first message must be connection.auth with a valid token (code -32003)` on the socket, `the server refused the token (HTTP 401) (code -32003)` on the WebSocket (`Unauthenticated`) | The token is not in the server's config | Use a token of the config; the server read its variables at startup |
| `at most <n> connections at once; try again once one ends` (`LimitReached`, `kind == "connections"`) | The tenant holds `max_connections` connections | Close one, or raise the tenant's `max_connections` |
| `at most <n> logins at once; try again once one ends` (`LimitReached`, `kind == "logins"`) | 32 socket connections wait to log in at once | Retry with backoff; the server is busy, not gone |
| `at most <n> agents at once; try again once one ends` (`LimitReached`, `kind == "agents"`) by `open_agent` | The tenant keeps `max_agents` agents open, across its connections | Close agents it no longer uses, or raise the tenant's `max_agents` |
| `stores.max_connections <n> is above the tenant's max_pool_connections <max>` (`NotAllowed`, `kind == "pool_connections"`) by `open_agent` | The config asks for a larger Postgres pool than the tenant may have | Ask for at most `<max>`, leave `max_connections` out, or raise the tenant's `max_pool_connections` (see [Stores](./stores.md#connections-and-pools) before going past 32) |
| `Server.connect over WebSocket needs the websocket extra` (`ImportError`) | `websockets` is not installed | `pip install 'linch-rust[websocket]'`, or use the socket |
| `a server reached with Server.connect has no process here` (`LinchError`) | `server.pid` on a remote server | Only a spawned server has a process here |

Refusals of `open_agent` and `run` on a remote server are `NotAllowed`
(-32004), whose `kind` names the check, and `LimitReached` (-32005) for
`max_agents` and `max_runs`; [Server](./server.md#what-admission-refuses-and-why)
lists them.

## Opening an agent

`open_agent` checks the whole config before anything runs.

| Message | Code | Cause | Fix |
|---|---|---|---|
| ``unknown field `<name>`, expected one of `namespace`, `spec`, ...`` | -32602 | A misspelt or unsupported field, at any level (also inside `spec`, `provider`, `permissions`, ...) | Use a field from [Agent config](./agent.md); keys never go in the config |
| ``missing field `project_root` `` | -32602 | `permissions` given without `project_root` | Add an absolute `project_root`, or leave `permissions` out for the default policy |
| ``unknown variant `skip-dangerous`, expected one of `default`, `accept_edits`, `skip_dangerous` `` | -32602 | Python Linch's mode spelling | Use `accept_edits` or `skip_dangerous` |
| `permission policy rejected: project_root must be an absolute path, got "<root>"` | -32011 | A relative `project_root` | Give an absolute path |
| ``unknown variant `<id>`, expected one of `deepseek`, `openai-chat`, ...`` | -32602 | An unknown provider `id` | Pick one from [Providers](./providers.md) |
| ``missing field `base_url` `` | -32602 | `vllm`, `sglang` or `llamacpp` without `base_url` | Add the server's URL, including `/v1` |
| `provider <id> needs its key in the environment variable <VAR>` | -32013 | The key is unset or blank in the server's environment; `data["env"]` names it | Export it before `Server()` starts, or pass it in `Server(env=...)` |
| `workspace unusable: <path>: no such file` | -32011 | `workspace` is missing or not a directory (a relative path resolves against the server's working directory) | Create the directory or give an absolute path |
| `tools: tool "<name>" is already registered` | -32602 | Two host tools with one name | Rename one with `@tool(name=...)` |
| `tools: tool name "<name>" must be 1-64 ASCII letters, digits, '_' or '-'` | -32602 | An invalid tool name | Rename it |
| `tool registration failed: tool "<name>" is already registered` | -32011 | A host tool takes the name of a built-in tool the config turns on (`Read`, `ls`, `TaskCreate`, ...) | Rename the host tool |
| `invalid agent: the model must not be blank` | -32011 | `spec.model` is empty | Set a model |
| `invalid agent: the namespace must not be blank` | -32011 | `namespace` is empty | Set a namespace |
| `invalid agent: workers need subagents` | -32011 | `workers` without `subagents: True` | Turn on `subagents` |
| `profile unusable: profile id must not be blank` | -32011 | A blank `profile.id` (or `version`, or `max_turns` below 1) | Fix the [profile](./profiles.md) |
| `deep agent unusable: a durable deep agent needs durable stores; use the Postgres stores, or set durable to false for ephemeral use` | -32011 | A built-in deep agent profile on memory stores | Use Postgres stores, or `"deep_agent": {"durable": False}` |
| `deep agent unusable: unknown deep-agent profile "<name>"; choose one of: balanced, coordinator, unbounded` | -32011 | A misspelt profile name | Use a built-in name or a full profile dict |
| `MCP server unusable: could not connect to MCP server "<name>": could not start the MCP server process: No such file or directory (os error 2)` | -32015 | The stdio `command` is not on the server's `PATH`; `data["server"]` names the entry | Use an absolute path, or include `PATH` in `Server(env=...)` |

A wrong key does not fail the open: no request is made until a run calls the
model. It shows as an `error` event in the run (an HTTP 401 or 403 `status`).

## Postgres

All of these are `OpenError` with code -32014, from the first agent that opens
on a DSN. The message never holds the DSN.

| Message (fragment) | Cause | Fix |
|---|---|---|
| `Postgres stores could not be opened: error communicating with database: cannot reach Postgres at <host>:<port> within 5s` | Nothing answered within 5 seconds: wrong host or port, the database is down, or a firewall | Check that the server's host can reach that host and port |
| `password authentication failed for user "<user>"` | Wrong user or password | Fix the credentials in the DSN |
| `database "<name>" does not exist` | The database in the DSN is missing | Create it; linch-rust creates tables, not databases |
| `error with configuration: relative URL without a base` | The DSN is not a URL | Use `postgres://user:pass@host:port/db` |
| `failed to lookup address information` | The host name does not resolve | Fix the host |
| `the migrations did not finish within 30s` | Another process holds the migration lock | Wait for it, or find the stuck migration |

A dropped connection in the middle of a run ends that run's stream with a
`RunError` (code -32020); the run stays at its last durable state and can be
resumed.

## Runs

These end a run's stream: iterating the `Run`, or `run.result()`, raises them.

| Exception | Message | Cause | Fix |
|---|---|---|---|
| `SessionBusy` (-32030) | `session <id> is busy with run <run id>` | The session has a run that is running or waiting for approval | Resume, approve or cancel `busy.run_id` first, or queue the prompt ([Runs](./runs.md#one-live-run-per-session)) |
| `RunError` (-32023) | `conflict: run <id> already exists in <namespace>` | `agent.run(..., run_id=...)` reused an id | Use a fresh id, or `resume` the existing run |
| `RunNotFound` (-32021) | `run <id> not found in namespace <namespace>` | No such run in the agent's stores; with memory stores, a different `Agent` handle started it, or the server restarted | With memory stores use the handle that started the run; with Postgres check the namespace and DSN |
| `RunTerminal` (-32026) | `run <id> is Completed and cannot continue` | `resume` or `approve` of a finished run; `data["status"]` is its status | Start a new run in the session |
| `ContractMismatch` (-32025) | `run <id> has contract <expected>, this agent has <found>` (the names of `data`'s keys) | The agent's spec, tools or callbacks differ from the ones the run started with | Reopen with the original config, or let the run finish first ([The run contract](./agent.md#the-run-contract)) |
| `NotSuspended` (-32027) | `run <id> is not awaiting approval; if an approve already committed its answers, continue the run with resume` | An approve sent again after its answers were committed (your process or the connection died) | Call `agent.resume(run_id)` ([Runs](./runs.md#approve)) |
| `PromptBlocked` (-32029) | the callback's reason, in `.reason` | A `user_prompt_submit` callback blocked the prompt | Nothing was written; change the prompt or the callback |

Raised by the call itself:

| Exception | Cause | Fix |
|---|---|---|
| `RunBusy` (-32002) | This server already drives that run, for example a `resume` while you still stream it | Wait for the stream to end, or `cancel()` it |
| `ConnectionClosed` | The `Agent` belongs to a server that has exited; a handle works only on the server that opened it | Open the agent again on a new `Server` |

A run that fails inside, without an exception, ends with an `error` event and a
`result` of subtype `error`:

| `error["message"]` | Cause | Fix |
|---|---|---|
| `max turns exceeded` (after a `loop_guard` event) | The run reached `spec.max_turns` | Raise the limit, or start a new run to continue |
| fragment: `provider returned HTTP <status>:` | The provider refused the call; `error["retryable"]` says whether trying again can help | Check the key and model for 401, 403 or 404; retry later for 429 and 5xx |
| `invalid provider request: scripted provider ran out of turns after <n>` | A scripted test needs more turns than it has | Add the missing turns |

## Tools

These are tool results the model reads (`ToolCallEndEvent.is_error` is true);
the run goes on.

| Result text | Cause | Fix |
|---|---|---|
| `<ExceptionType>: <message>` | The host tool raised | Fix the tool, or raise a message the model can act on |
| `Host tool "<name>" timed out after <n>ms` | The host tool did not answer within its `timeout` (300 s by default); its task was cancelled | Raise `@tool(timeout=...)`, or make the tool faster |
| `Input for tool "<name>" does not match its schema:` followed by the problems | The model's input does not fit the schema | Usually the model corrects itself; check the schema and description |
| `Tool "<name>" is not registered` | The model called a tool the agent does not have | Check the tool list in the `init` event |
| `Tool call denied: <reason>` | `Deny(reason)` on approval | Expected |
| `Tool call denied: <tool> denied by <kind> rule in layer "<layer>"` | A permission rule denied the call | Change the rule if the call should run ([Tools](./tools.md#permission-rules)) |
| `Read <path> before changing it.` | `Write` over an existing file or `Edit` without a `Read` in this session, or after `close_session` or a restart | Expected; the model reads, then writes |
| `Tool execution was interrupted before a result was recorded; not re-running automatically on resume.` | The process died while the call ran | Check whether the effect happened before acting again |

`@tool` itself raises at import time: `ValueError` for a scope that is not
`read`, `write` or `exec` or a timeout that is not positive, and `TypeError`
for a function that is not `async` or a parameter it cannot type (`pass
input_schema`).

Calls that keep asking: in mode `default` every `write` and `exec` call asks,
and in `accept_edits` a write outside the project root asks. Use a `tool`
allow rule, `auto_allow`, a `permission_request` callback, or a mode that fits
([Tools](./tools.md#permissions)).

## Related pages

- [Runs](./runs.md): the error table and resume rules.
- [Agent config](./agent.md): every field.
- [Providers](./providers.md): keys and endpoints.

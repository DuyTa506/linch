# Troubleshooting

Use this page to match an error from `linch_rust` to its cause and fix.

[<- Usage guide](./README.md)

Messages are quoted as the client shows them. Parts in `<...>` vary; a row
marked "fragment" quotes only the fixed part of a longer message. Every
server error is an `RpcError` with `code`, `message` and `data`; the
exception class tells you its family:

| Class | Codes | Raised by |
|---|---|---|
| `ProtocolError` | -32700, -32600, -32601, -32602, -32603 | A config or declaration the server cannot read (-32602); a method the server binary lacks (-32601: the binary is older than the client); a bug in the server (-32603: report it with the server's stderr) |
| `OpenError` | -32010 to -32015 | `open_agent`: the config, provider, stores or an MCP server failed; -32012 is a run contract that could not be digested (report it) |
| `RunError` and its subclasses | -32020 to -32034 and -32038 | The end of a run's stream, or `agent.events` |
| `AgentNotFound`, `RunBusy` | -32001, -32002 | The call itself |
| `Unauthenticated`, `NotAllowed`, `LimitReached` | -32003, -32004, -32005 | A remote server's token, tenant checks and caps ([A remote server](#a-remote-server-serverconnect)) |
| `ProviderInfoError` and its subclasses | -32035 to -32037 | `provider.models` and `provider.model`, asked before an agent opens |
| `SchedulerError` and its subclasses | -32040 to -32044 | Schedule and job calls ([Scheduler](#schedules-and-background-runs)) |
| `WorkflowError` and its subclasses | -32045 to -32049 | Workflow calls ([Workflows](#workflows)) |
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

The server writes its own log to stderr: warnings and errors by default,
more with `LINCH_LOG=info` (or `debug`). A failed scheduled job, a retry, a
pruning error and a workflow journal failure show there and nowhere else
([Server](./server.md#logging)). A server started by `Server()` writes it to
the stderr of your process.

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
| `at most <n> connections at once; try again once one ends` (`LimitReached`, `kind == "connections"`) | The tenant holds `max_connections` connections, one of them maybe a client that vanished without closing, which the server ends after about a minute ([liveness](./server.md#liveness)) | Close one, or raise the tenant's `max_connections`; retry after a minute if a client died |
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
| `budget.max_cost_usd needs a price for the model "<model>", which has none: set ...` | -32011 | A USD cap on a model with no price, neither in the built-in table nor in `pricing`: the primary or a `fallback_models` entry (vLLM, DeepSeek, SGLang and llama.cpp have no built-in price). Before this was refused the cap never tripped | Set [`pricing`](./agent.md#budget) for the model, or cap `max_tokens` |
| `call_timeout_ms must be at least 100 (milliseconds)` | -32011 | `spec.call_timeout_ms` under 100 (`0` ended every tool call with a timeout at once) | Use 100 or more, or leave it out |
| `a tenant agent needs a bound on its runs: set spec.max_turns or a spec.budget cap (max_tokens or max_cost_usd), ...` (`NotAllowed`, `kind == "run_bound"`) | -32004 | A tenant's agent with neither `max_turns` nor a token or USD cap, `profile` and `deep_agent` profile included | Set `max_turns` or a budget cap ([Server](./server.md#what-admission-refuses-and-why)) |
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
| `SessionBusy` (-32030) | `session <id> is busy with run <run id>` | The session has a run that is running or waiting for an approval or an answer | Resume, approve, answer or cancel `busy.run_id` first, or queue the prompt ([Runs](./runs.md#one-live-run-per-session)) |
| `RunExists` (-32023) | `conflict: run <id> already exists in <namespace>` | `agent.run(..., run_id=...)` reused an id | Use a fresh id, or `resume` the existing run |
| `RunNotFound` (-32021) | `run <id> not found in namespace <namespace>` | No such run in the agent's stores; with memory stores, a different `Agent` handle started it, or the server restarted | With memory stores use the handle that started the run; with Postgres check the namespace and DSN |
| `RunTerminal` (-32026) | `run <id> is Completed and cannot continue` | `resume`, `approve` or `answer` of a finished run, a cancelled one included (`data["status"]` is `aborted`); `data["status"]` is its status | Start a new run in the session |
| `ContractMismatch` (-32025) | `run <id> has contract <expected>, this agent has <found>` (the names of `data`'s keys) | The agent's spec, tools, callbacks or (when it is not the default) permission policy differ from the ones the run started with; after an upgrade, a run started by an older binary under a non-default policy | Reopen with the original config, or let the run finish first; a suspended run can still be ended with `agent.cancel` ([The run contract](./agent.md#the-run-contract)) |
| `NotSuspended` (-32027) | `run <id> is not awaiting this approval or answer; if an approve or answer already committed, continue the run with resume` | An approve or answer sent again after it was committed (your process or the connection died), or an answer to a run waiting for an approval | Call `agent.resume(run_id)` ([Runs](./runs.md#approve)) |
| `PromptBlocked` (-32029) | the callback's reason, in `.reason` | A `user_prompt_submit` callback blocked the prompt | Nothing was written; change the prompt or the callback |
| `InvalidOutputSchema` (-32031) | `run <id> was not started: its output schema is unusable: <reason>` | `agent.run(..., output_schema=...)` got a schema that does not compile, or whose root is not `{"type": "object"}` | Nothing was written; fix the schema ([Structured output](./structured-output.md)) |
| `RunLeased` (-32033) | `run <id> is held by "<owner>" for up to <duration> more` | Another server (another agent handle) drives the run on Postgres, or crashed less than a lease ago; from `agent.cancel`, an approve elsewhere has just claimed the suspended run | Wait for that run to stop, or retry after `.expires_in` seconds ([Stores](./stores.md#run-leases)) |
| `LeaseLost` (-32022) | `lease on run <id> lost (epoch <n>)` | Another server took the run over (this one stalled past its lease) or failed it; from `agent.cancel`, an approve or answer claimed the run after the cancel did, and it goes on | Nothing to fix here: the run's new driver has it. If this server was paused, check for long GC or network pauses, or raise `stores.lease_ttl_ms` |
| `InvalidAnswer` (-32032) | `run <id> cannot take this answer: <reason>` (the reason in `.reason`) | `agent.answer` named a request that is not pending, left a question unanswered, or gave a list to a single-select question | Nothing was written and the run still waits; answer again ([Tools](./tools.md#ask-the-user)) |
| `InvalidToolChoice` (-32034) | `run <id> was not started: its tool_choice is unusable: <reason>` | `agent.run(..., tool_choice=...)` names a tool the run does not offer, or the provider refuses it | Nothing was written; fix the choice ([Structured output](./structured-output.md)) |
| `InvalidSteer` (-32038) | `run <id> cannot take this steer: <reason>` (`.reason`) | `agent.steer` with blank text, text over 16 KiB, an unusable id, or a run that is idle (queued, or running with no server driving it) | Nothing was written; fix the text, or resume the run first ([Runs](./runs.md#steer)) |
| `LimitReached` (-32005), `kind == "steers"` | `at most 16 steers at once; try again once one ends` | The run has 16 steers it has not read yet | Wait for the run's next step, then steer again |
| `RunError` (-32020, -32024, -32028) | the store's or the journal's own text | A store failure, a failed journal write, or durable state that does not fit together; a dropped database connection mid-run is -32020 | The run is at its last durable state: resume it; report -32028 |

Raised by the call itself:

| Exception | Cause | Fix |
|---|---|---|
| `RunBusy` (-32002) | This server already drives that run, for example a `resume` while you still stream it | Wait for the stream to end, or `cancel()` it |
| `ConnectionClosed` | The `Agent` belongs to a server that has exited; a handle works only on the server that opened it | Open the agent again on a new `Server` |

`agent.cancel` and `run.cancel()` of a suspended run raise `RunNotFound`,
`RunLeased` and `LeaseLost` themselves, with the causes above. `False` from
them is not an error: the run had already finished, or it is running and
this connection does not drive it ([Runs](./runs.md#cancel-a-suspended-run)).

A run that fails inside, without an exception, ends with an `error` event and a
`result` of subtype `error`:

| `error["message"]` | Cause | Fix |
|---|---|---|
| `max turns exceeded` (after a `loop_guard` event) | The run reached `spec.max_turns` | Raise the limit, or start a new run to continue |
| `max_cost_usd cannot count a call the pricing has no price for; set a price for the model that served it or cap max_tokens` (`BudgetAccountingError`) | A cost-capped run whose provider served a model the pricing lacks (an id the provider reports, not `spec.model`) | Key `pricing` by the ids the provider reports, or cap `max_tokens` ([Agent config](./agent.md#budget)) |
| fragment: `provider returned HTTP <status>:` | The provider refused the call; `error["retryable"]` says whether trying again can help | Check the key and model for 401, 403 or 404; for 408, 429 and 5xx the run already retried up to [`spec.provider_retry`](./providers.md#failures-and-retries)'s `max_attempts`; add `fallback_models` or retry later |
| fragment: `prompt exceeds the model's context window:` (`kind` `context_length`) | The provider refused the prompt as longer than the model's context window. Before this was recognized it read `provider returned HTTP 400:` | Set [`spec.compaction`](./memory.md#when-the-prompt-does-not-fit): the run then shortens its view and calls again |
| `the prompt exceeds the model's context window, and the compaction it forced could not shorten it` | With `spec.compaction` set, nothing was left to fold, `max_forced` was spent, or the forced summary failed for a reason other than size | Lower `keep_recent_turns`, turn on `micro`, raise `max_forced`, or keep tool results and `context_build` blocks smaller |
| `the prompt exceeds the model's context window of <n> tokens and nothing is left to shorten: the system prompt and tools take about <a> estimated tokens and the conversation <b> ...` (`kind` `context_length`) | With `spec.compaction` set, the run summarized and clipped as far as it goes (a forced summary request refused at every size, or nothing left to fold or clip) and the prompt still does not fit: the fixed part (system prompt, tools, the last turn) is over the window. With `max_forced` spent and history still to fold, the message says `compaction.max_forced (<n>) is spent` instead of `nothing is left to shorten` | Shrink the system prompt or the tool set, or use a model with a larger window (raise `max_forced` when it was the spent bound); the sizes are the harness's estimates, which can be up to 2x under the provider's count |
| fragment: `provider connect error:` (`kind` `connect`) | The connection was never made (refused, DNS, TLS, connect timeout), so the request never went out | Check the provider's `base_url` and the network; the run already retried it |
| fragment: `provider transport error:` (`kind` `transport`) | The request went out but no answer came back, or the stream broke | The run already retried it; under a capped `budget` it cannot, since the call may have been billed |
| `invalid provider request: scripted provider ran out of turns after <n>` | A scripted test needs more turns than it has | Add the missing turns |

## Schedules and background runs

| Raised | Cause | Fix |
|---|---|---|
| `NotBackground` (-32040) | `schedule.create` or `job.enqueue` for an agent that cannot run in the background; `.reason` is `memory_store`, `host_tools`, `callbacks` or `remote_execution` (a `remote` execution backend answers on the client's connection) | Use Postgres stores, no host tools or callbacks, and a `local`, `sandbox` or `docker` backend ([Scheduler](./scheduler.md#which-agents-can-run-in-the-background)) |
| `ScheduleNotFound` (-32041), `ScheduleExists` (-32042), `JobNotFound` (-32043) | An unknown schedule, a taken schedule id, an unknown or pruned job | Check the id and the agent's namespace |
| `InvalidSchedule` (-32044) | A cron pattern that does not parse or never fires, an unknown time zone, an interval out of range, or both or neither of `cron` and `interval_ms`; `.reason` | Fix the timing |
| A job fails with a reason naming `max_turns` and a budget | A stored definition made before a tenant had to bound its runs (`run_bound`); each fire of its schedule fails the same way, and `job.list` shows the reason | Set `max_turns` or a budget cap, open the agent again and schedule it anew |
| A job fails with `the agent definition does not decode` | The stored definition no longer decodes; the cause is in the server's log (`LINCH_LOG`) | Open the agent again and schedule it anew |

## Workflows

| Raised | Cause | Fix |
|---|---|---|
| `WorkflowHeld` (-32045) | Another client, connection or server drives the workflow, or a client that crashed has not been noticed yet (about a minute: [liveness](./server.md#liveness)) | Retry after a minute |
| `WorkflowBudgetExceeded` (-32046) | The `budget` is spent; or `data["kind"] == "unpriced"`: a `max_cost_usd` budget on a model with no price | Raise the budget and start the same `run_id` again; set `pricing` or cap `max_tokens` |
| `WorkflowCallFailed` (-32047) | A `wf.agent` call's run failed, was aborted, timed out or waits for an approval; `.kind` | Read `.kind`; the finished calls are journaled |
| `WorkflowNotFound` (-32048) | A call for a workflow this connection does not drive | Send `workflow.start` first (`run_workflow` does) |
| `WorkflowNoJournal` (-32049) | `resume` answers for a workflow whose journal holds nothing, as with a memory-store agent on a new connection | Start it without `resume`, or use Postgres stores |
| `NotAllowed` (-32004), `kind == "workflow_principal"` | The id was started for another principal | Pass the same principal, or start a new `run_id` ([Workflows](./workflows.md#acting-for-a-principal)) |
| `NotAllowed` (-32004), `kind == "workflow"` | The tenant has no `allow.workflow` | Ask the operator ([Workflows](./workflows.md#admission)) |
| `ConnectionClosed` after a long synchronous host tool | A WebSocket client whose event loop was blocked for over 60 s answered no ping, so the server ended the connection | Run a long blocking tool in a thread (`asyncio.to_thread`) ([Liveness](./server.md#liveness)) |

## Provider questions

| Raised | Cause | Fix |
|---|---|---|
| `ModelsUnsupported` (-32035) | `provider.models` or `provider.model` for a provider with no model list (`scripted`); `.provider` | Ask another provider |
| `ModelNotFound` (-32036) | The provider's list lacks the model; `.truncated` says the list was cut short | Check the id; with `.truncated` it may exist past the cut |
| `ProviderRequestFailed` (-32037) | `.kind` is `unreachable`, `status` (with `.status`), `invalid_request` or `other`; the message is fixed per kind and never holds the key, URL or the vendor's words | Check the `base_url`, the key and the network; the server's log has more with `LINCH_LOG=info` |

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
| `Read this file again before editing it.` / `... before writing it.` | The file changed on disk after the model read it | Expected; the model reads again, then retries |
| `Tool execution was interrupted before a result was recorded; not re-running automatically on resume.` | The process died while the call ran | Check whether the effect happened before acting again |
| `Background process <id> no longer exists in this session: ...` (code `no_such_process`) | `BashOutput` or `BashKill` named an id this agent handle does not know: the session was closed, the server restarted, or another handle started it | Expected after a restart; the model starts the command again ([Tools](./tools.md#background-commands)) |
| `4 background processes already run in this session; ...` (code `too_many_processes`) | A fifth background `Bash` in one session | The model reads or kills one first |
| `AskUser is not available here: this run cannot wait for the user...` | A subagent called `AskUser` | Expected; only the top-level run can ask |

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

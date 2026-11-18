//! The Linch JSON-RPC 2.0 protocol, with servers for byte streams
//! ([`serve`], [`serve_lines`]) and message transports
//! ([`serve_messages`]), and a [`Client`].
//!
//! A process (the `linch-server` sidecar) opens agents from configs and
//! drives their runs; the client starts, approves, cancels and replays runs
//! and receives every envelope in a notification. Everything inside a turn
//! runs in the server; it calls back into the client, on the same
//! connection, only for what the client declared: host tools, and
//! intercept callbacks at the occurrences their matchers accept.
//!
//! # Transports and access
//!
//! Each connection has its own agents, runs and server requests. Handles
//! are numbered per connection, so `agent-1` names the connection's own
//! first agent and never another connection's; `run.cancel` reaches only
//! the runs its own connection drives, or, given an agent handle, a
//! suspended run in that agent's stores.
//!
//! - **stdio** ([`serve`]): `linch-server` with no arguments. The client
//!   started the server and is trusted ([`Access::Local`]): no login, no
//!   tenant, no allowlists.
//! - **Unix socket** ([`serve_lines`] with [`Access::Login`]): one message
//!   per line, as on stdio. The first message must be
//!   **`connection.auth`** `{token}` ([`AuthParams`](protocol::AuthParams)),
//!   answered with [`Authenticated`](protocol::Authenticated)
//!   `{"tenant": "acme"}`. Any other first message or an unknown token gets
//!   `-32003` and the connection closes, and so does 10 s of silence. At most
//!   [`MAX_PENDING_LOGINS`] connections wait for their login at once; one
//!   more gets `-32005` (`kind: logins`) at once and closes.
//! - **HTTP** ([`serve_messages`] with [`Access::Tenant`]): `GET /rpc` with
//!   `Authorization: Bearer <token>` upgrades to a WebSocket carrying one
//!   message per text frame (a binary frame that is not UTF-8 ends the
//!   connection). A missing or unknown token gets `401` before the upgrade,
//!   and a tenant at its connection cap `429`: the transport takes the
//!   connection's place with [`Tenant::connect`] before it upgrades.
//!
//! `connection.auth` anywhere but first on the socket is `-32600`. Tokens
//! are compared in constant time and never logged or echoed. A remote
//! connection acts for the token's [`Tenant`], and every request passes
//! its checks before anything runs (no key is read, no process started, no
//! database connected for a refused one):
//!
//! - `agent.open`: the `namespace` is the tenant's name or starts with
//!   `<tenant>/`. Stores are scoped by namespace, so on a shared Postgres
//!   another tenant's runs and sessions look missing (`-32021`).
//! - `run.start`, `run.resume`, `run.approve`, `run.answer`, `run.steer`, `schedule.create`,
//!   `job.enqueue`: a `principal`, if given, has
//!   the tenant's name as its `tenant`. The server does not fill one in.
//! - `agent.open`: the config passes the tenant's [`Allowlists`]:
//!   - the scripted provider only if allowed, and a client-chosen provider
//!     `base_url` (always present for vllm, sglang and llama.cpp) only at
//!     an allowed origin, since the server's key goes there;
//!   - a permission mode in the allowed list (by default `default` and
//!     `accept_edits`); `auto_allow` entries and rules that allow only
//!     with `skip_dangerous` in it, since they let calls run unasked, and
//!     so too `tasks` and `deep_agent`, which auto-allow the task tools;
//!   - a `workspace` and a `permissions.project_root` only as existing
//!     directories under an allowed root, each replaced by its canonical
//!     path (without a root, a config keeps the default policy);
//!   - with skills or subagents on, a `config_dir` (relative to the
//!     workspace) only as an existing directory under an allowed root, and
//!     the default `.linch` only there or missing, so neither leads, by an
//!     absolute path, `..` or a symlink, to another tenant's files; it is
//!     replaced by the checked path;
//!   - `Bash` only when the tenant allows exec tools; otherwise the config's
//!     `bash` is turned off and the agent opens without it;
//!   - a Postgres DSN only with an allowed host, port, socket, database,
//!     user, options and TLS files (`sslrootcert`, `sslcert`, `sslkey`),
//!     and an `sslmode` at least as strict as the entry's;
//!   - an MCP stdio server only with the `command` and `args` of an entry,
//!     setting only the `env` names that entry lists ([`McpCommand`]); an
//!     MCP HTTP URL only at an allowed origin.
//!
//! A refusal is `-32004` with `data.kind` naming the check: `namespace`,
//! `principal`, `provider`, `provider_base_url`, `permission_mode`,
//! `auto_allow`, `permission_rule`, `workspace`, `project_root`,
//! `config_dir`, `postgres`, `mcp_command`, `mcp_env` or `mcp_url`.
//!
//! A tenant also has caps, counted across all its connections: at most
//! `max_runs` runs driven at once (default [`DEFAULT_MAX_RUNS`]; a run
//! holds its place until its `run.end`, or, when it suspends with
//! background subagents still running, until they end or an approve or
//! resume of it takes the place over), `max_agents` agents open (default
//! [`DEFAULT_MAX_AGENTS`]; until `agent.close` or the connection's end has
//! closed its MCP servers) and `max_connections` connections (default
//! [`DEFAULT_MAX_CONNECTIONS`]). Past a cap the request is refused with
//! `-32005` and `data: {kind: "runs" | "agents" | "connections", max}`;
//! a refused socket login gets that error under its `connection.auth` id
//! and closes (on HTTP, the `429` above). A local connection has none.
//!
//! # Framing
//!
//! One JSON-RPC message per line, UTF-8, ending in `\n`; blank lines are
//! skipped. A line over [`MAX_LINE_BYTES`] is discarded and reading goes
//! on: a request gets `-32600` under its `id` when the `id` is in its first
//! 64 KiB, else under a null id; an answer fails the server request it
//! names. A line that is not UTF-8 gets `-32700` and reading goes on.
//! Message transports apply the same limit per message. Batches (arrays)
//! are not supported and get one `-32600` error. `params` must be an
//! object (by-name); an absent `params` reads as `{}`. A request without an
//! `id` is a notification: the protocol defines none from client to
//! server, and JSON-RPC forbids answering one, so it is dropped silently.
//!
//! Reading is strict. A request object with a member other than
//! `jsonrpc`, `id`, `method` and `params`, a `jsonrpc` other than `"2.0"`,
//! or an `id` that is not a string, number or null is `-32600`. Params with
//! an unknown, missing or mistyped field are `-32602`. Every result is an
//! object. Calls run concurrently, so responses may arrive in any order;
//! match them by `id`. At most 64 requests run at once per connection;
//! past that, reading waits until one is answered.
//!
//! Both directions carry requests. The server reads a line with a `method`
//! as a request and a line with no `method` but a `result` or `error` as
//! the answer to one of its own requests; it never answers an answer. The
//! client reads each server line as exactly one of a response, a
//! notification or a request ([`Message`]); it keeps at most
//! [`NOTIFICATION_CAPACITY`] unread notifications, then stops reading.
//!
//! # Methods
//!
//! **`agent.open`**: params are an [`AgentConfig`](linch::AgentConfig) plus
//! optional `tools` and `callbacks` lists
//! ([`OpenParams`](protocol::OpenParams)):
//!
//! ```json
//! {"namespace": "acme",
//!  "spec": {"model": "deepseek-flash", "system_prompt": "Be brief."},
//!  "provider": {"id": "deepseek"},
//!  "stores": {"type": "postgres", "dsn": "postgres://linch@db/linch"},
//!  "workspace": "/srv/acme",
//!  "permissions": {"mode": "accept_edits", "project_root": "/srv/acme"},
//!  "mcp_servers": [{"name": "git", "transport": {"type": "stdio",
//!                   "command": "git-mcp"}}],
//!  "tools": [{"name": "crm_lookup", "description": "Find a customer.",
//!             "input_schema": {"type": "object",
//!                              "properties": {"email": {"type": "string"}}},
//!             "scope": "read"}],
//!  "callbacks": [{"point": "permission_request", "policy_id": "acme-approvals",
//!                 "policy_version": 3, "matcher": ["Bash", "mcp__git__*"],
//!                 "timeout_ms": 5000}]}
//! ```
//!
//! `stores` defaults to `{"type": "memory"}`: stores owned by this agent
//! handle, so resume and approve must use the same handle. Local agents on
//! one Postgres target share one pool across all connections of a server
//! process (the [`PgPools`](linch::PgPools) in their [`Access::Local`]);
//! a tenant's agents share pools of the tenant's own,
//! each at most its `max_pool_connections`. Only an open that finds no
//! pool in use connects,
//! giving up after 5 s, and runs the migrations, giving up after 30 s;
//! `agent.close` never closes a pool another agent uses. Provider keys are
//! read from the server's environment (`DEEPSEEK_API_KEY`, ...; see
//! [`ProviderConfig`](linch::ProviderConfig)) and are never params. The
//! config's `mcp_servers` are connected in order before the result is sent.
//! The result is [`AgentOpened`](protocol::AgentOpened):
//! `{"agent": "agent-1", "contract": "<hex digest>", "skipped"?: [...]}`;
//! `skipped` lists each skill directory or subagent file of the config
//! directory that was left out, `{path, reason, detail}`
//! ([`Skipped`](protocol::Skipped)), and is absent when none was. The contract covers
//! the spec, every tool registered at open (host, workspace, then MCP),
//! each with its scope, and the hook policies; a tool an MCP server adds
//! later is outside it. A
//! handle lives until `agent.close` or the end of its connection.
//!
//! Each of `tools` ([`HostToolSpec`](protocol::HostToolSpec)) is
//! `{name, description, input_schema, scope, timeout_ms?, idempotent?}`. `scope` is
//! required: `read`, `write` or `exec`. It decides approval like any tool's
//! (in mode `default`, `write` and `exec` calls ask), and only `read` calls
//! run in parallel. `timeout_ms` defaults to 300 000. `idempotent` (default
//! false) says a repeat has no effect beyond its result: only such a tool is
//! retried by `spec.tool_retry`. A host tool is run with `tool.call` on the
//! connection that opened the agent.
//!
//! Each of `callbacks` ([`CallbackSpec`](protocol::CallbackSpec)) is
//! `{point, policy_id, policy_version, matcher?, timeout_ms?, fail_mode?}`
//! and is answered with `hook.call`. `point` is one of `user_prompt_submit`,
//! `context_build`, `pre_tool_use`, `permission_request`, `post_tool_use`,
//! `stop`, `post_turn`, `tool_execute`; callbacks at one point run in declaration
//! order. `matcher` is a list of tool-name globs (`*`, `?`), allowed only at
//! the four tool points; it is evaluated in the server, so an occurrence it
//! rejects never crosses the connection. `timeout_ms` defaults to 30 000. A
//! callback that errors, answers badly or times out is handled by its
//! `fail_mode`: `closed` turns it into the point's blocking answer (the
//! call is blocked or denied, the result withheld; at `stop` the run ends
//! failed with a hook error),
//! `open` skips it. The default is `closed` at `pre_tool_use` and
//! `permission_request` and `open` elsewhere; `context_build` and
//! `post_turn` cannot fail closed. Each `(point, policy_id,
//! policy_version)` enters the contract in declaration order, duplicates
//! included, so reordering callbacks changes it too; bump `policy_version`
//! whenever a callback's behaviour, matcher, timeout or fail mode changes.
//!
//! Invalid `tools` or `callbacks` entries are `-32602`.
//!
//! **`agent.close`** `{agent}` ([`CloseParams`](protocol::CloseParams))
//! frees a handle: its stores and its MCP servers, which are disconnected
//! and awaited before the result `{}`. While the connection drives a run of
//! that agent it is refused with `-32002`; cancel the run or wait for its
//! `run.end`.
//!
//! **`session.close`** `{agent, session_id}`
//! ([`SessionCloseParams`](protocol::SessionCloseParams)) tells the agent
//! the client is done with a session and answers `{}`: the workspace tools
//! forget what the session read, so a later write there must read the file
//! again, and its background `Bash` commands are killed. Nothing is
//! written; the session and its runs stay, and a run of it still in
//! progress just has to read again before it writes.
//!
//! **`run.start`** `{agent, session_id, prompt, run_id?, principal?, trace_id?, output_schema?, tool_choice?}`
//! ([`StartParams`](protocol::StartParams)) starts a run; the session is
//! created when missing. `run_id` names the new run (a fresh uuid when
//! absent), so a client can resume it even if it never saw the answer; an
//! id already stored ends the run with `-32023` and writes nothing. A
//! session has at most one run that is not terminal: a start on a session
//! whose run is still going or waiting for approval ends with `-32030`,
//! naming that run, and writes nothing. `principal` is
//! `{tenant, user, roles?}`. `trace_id` is the host's trace id: stored with
//! the run and echoed on every `init` and `result` of it, its tool calls'
//! context and its subagents' runs, a resume or approve included.
//! `output_schema` (`{name, schema, strict?, description?}`) replaces the
//! agent's `spec.output_schema` for this run: the `init` event stores it
//! and a resume or approve uses it again. One that does not compile, or
//! whose root is not an object, ends the run with `-32031` and writes
//! nothing. The result's `structured_output` is the checked answer.
//! `tool_choice` (`{"type": "auto" | "none" | "required"}` or
//! `{"type": "tool", "name"}`) replaces the agent's `spec.tool_choice` for
//! this run, stored the same way; one that names a tool the run does not
//! offer, or that the agent's provider refuses, ends the run with `-32034`
//! and writes nothing.
//!
//! **`run.resume`** `{agent, run_id, principal?}`
//! ([`ResumeParams`](protocol::ResumeParams)) continues a run after a crash
//! or a dropped stream; a run awaiting approval suspends again. A
//! `run.resume`, `run.approve` or `run.answer` without `principal` acts for
//! the principal the run was started with; one with a `principal` is
//! checked as `run.start`'s is and acts for it instead.
//!
//! **`run.approve`** `{agent, run_id, decisions, principal?}`
//! ([`ApproveParams`](protocol::ApproveParams)) answers a suspended run's
//! pending calls and continues it. `decisions` maps each `tool_use_id` (from
//! the `suspended` envelope's `pending_request_ids`) to
//! `{"decision": "allow"}` or `{"decision": "deny", "reason": "..."}`. Only
//! a run awaiting approval takes it: once an approve has committed its
//! answers, the same approve sent again (after a dropped connection or a
//! crash) ends with `-32027` in `run.end` and writes nothing. Continue
//! that run with `run.resume`, which keeps the committed decisions.
//!
//! **`run.answer`** `{agent, run_id, answers, principal?}`
//! ([`AnswerParams`](protocol::AnswerParams)) answers a run suspended for
//! input (an `AskUser` call; `suspended` with status `awaiting_input`) and
//! continues it. `answers` maps each pending `request_id` (the
//! `input_request`'s, which is the call's `tool_use_id`) to
//! `{"accepted"?: bool, "answers": {question_id: "label" | ["label", ...]},
//! "note"?: "..."}`; `accepted: false` declines. Each answer must fit its
//! request's questions (every question answered, a list for a multi-select
//! one), or the run ends with `-32032` and stays suspended. As with
//! `run.approve`, only a run awaiting input takes it, and once an answer
//! has committed, the same answer sent again ends with `-32027`; continue
//! that run with `run.resume`.
//!
//! These four answer [`RunAccepted`](protocol::RunAccepted)
//! `{"run_id": "..."}` at once, before any notification of that run; the
//! run's outcome, errors included, then arrives as notifications. A
//! `run_id` the connection already drives is refused with `-32002`.
//!
//! **`run.steer`** `{agent, run_id, text, id?, principal?}`
//! ([`SteerParams`](protocol::SteerParams)) sends text to a run that is
//! going on or waits for an approval or an answer, answered `{queued: true,
//! id}` ([`Steered`](protocol::Steered)) once the steer is in the run's
//! journal (a run this process drives writes it at its next step; a
//! suspended run is claimed for the write and stays suspended). The run
//! logs it as a user message (`subtype` `alignment`, `steer_id`) after the
//! tool results of the current batch and before its next model call, or
//! before it would end on a text answer, which then takes another turn;
//! `user_prompt_submit` callbacks see it with `source` `"steer"`. `id`
//! names the steer: sent again, it is taken once. A run another process
//! drives is `-32033`, a finished one `-32026`, an unknown one (or one of
//! another tenant) `-32021`, a blank or over-long (16 KiB) text or an
//! unusable id, or an idle run (queued, or running with no server driving it:
//! resume it first) `-32038`, and a run with 16 steers waiting or unread
//! `-32005` (`kind` `steers`), answered at once. It is not routed to another
//! server.
//!
//! **`run.cancel`** `{run_id, agent?}` ([`CancelParams`](protocol::CancelParams))
//! aborts a run the connection drives: it ends with a `result` envelope of
//! subtype `aborted`. With `agent`, a run the connection does not drive
//! that waits for an approval or an answer (suspended here, in another
//! process, or before a restart) is cancelled through that agent's
//! stores: its pending calls get aborted results, it ends with a `result`
//! of subtype `aborted` and status `aborted`, readable with `run.events`.
//! The result is `{"cancelled": true}`, or `{"cancelled": false}` when
//! there was nothing to cancel (the run is not driven here and, with
//! `agent`, is finished or not suspended); that is not an error. A store
//! cancel can fail like a resume: `-32021` for an unknown run, `-32033`
//! while another process drives it, `-32022` when a racing approve took
//! it over.
//!
//! **`provider.capabilities`** `{provider, model}`, **`provider.models`**
//! `{provider}` and **`provider.model`** `{provider, model}` take a
//! `provider` as an agent config has it (`{"id": "vllm", "base_url": ...}`),
//! so a client can ask before it opens an agent. `provider.capabilities`
//! answers [`ProviderCapabilities`](linch::ProviderCapabilities) from the
//! adapter without a key, after a self-hosted server's one window probe;
//! `provider.models` returns `{"models": [ModelInfo...], "truncated"?: true}`
//! from the vendor's list (`truncated` when it had more pages than the
//! server follows) and `provider.model` one entry, with the server's key;
//! `provider.capabilities` sends no key. A tenant needs `provider_info` and
//! a `base_url` allowed in origin and path (`-32004`, `kind`
//! `provider_info` or `provider_base_url`), and may make 10 `provider.models`
//! and `provider.model` calls a minute (`-32005`, `kind` `provider_calls`).
//!
//! **`run.events`** `{agent, run_id, after_seq?}`
//! ([`EventsParams`](protocol::EventsParams)) returns
//! `{"events": [<envelope>...]}`: the run's durable envelopes with a seq
//! greater than `after_seq` (default 0), in order, for replay after a
//! reconnect.
//!
//! ## Scheduler
//!
//! These methods queue runs that a worker runs in the background, never
//! this connection: `linch-server` with a `worker` section, on the same
//! database and schema (a stdio server never runs them). Each names an
//! open `agent` with Postgres stores, no host tools or callbacks and no
//! `remote` execution backend for its `Bash` (else `-32040`, `data.reason`
//! `memory_store`, `host_tools`, `callbacks` or `remote_execution`). Its namespace scopes the schedules and jobs, and its
//! config as opened, without its store DSN, is stored with each one: the
//! worker opens it again for every attempt and checks it against the
//! tenant's allowlists again. Its secrets stay in that stored config,
//! which is deleted once nothing names it. A `principal`, as for
//! `run.start`, is stored too and every attempt acts for it. Times are
//! RFC 3339 strings in UTC.
//!
//! **`schedule.create`** `{agent, prompt, id?, cron?, timezone?,
//! interval_ms?, session_id?, queue?, principal?}`
//! ([`ScheduleCreateParams`](protocol::ScheduleCreateParams)) stores a
//! schedule and returns it ([`ScheduleInfo`](protocol::ScheduleInfo)).
//! Exactly one of `cron` (five fields, or six with seconds, read in the
//! IANA `timezone`, default `UTC`) and `interval_ms`, else `-32044`. With a
//! `session_id` every fire is a new run in that session (a heartbeat), and
//! a fire is skipped while an earlier fire's job is unfinished or the
//! session has a live run; without one each fire starts the session
//! `schedule:<id>:<fire time>`, and a fire is skipped while an earlier
//! fire's job is unfinished or the tenant holds its `max_queued_jobs`.
//! Fires missed while every worker was down coalesce into one; the rest
//! count in `skips`. A tenant needs `max_schedules` (`-32004`, `kind: schedules`;
//! `-32005` at the cap, counted over its namespaces) and fires at least
//! `min_schedule_interval_ms` apart (`-32004`, `kind: schedule_interval`).
//! A taken `id` is `-32042`.
//!
//! **`schedule.list`** `{agent}` returns `{"schedules": [...]}` by id.
//! **`schedule.pause`**, **`schedule.resume`** `{agent, id}` return the
//! schedule (`-32041` when there is none); a resumed schedule fires from
//! now on, without the fires it missed while paused.
//! **`schedule.delete`** `{agent, id}` returns `{"deleted": bool}`; its
//! queued and running jobs go on.
//!
//! **`job.enqueue`** `{agent, session_id, prompt, queue?, principal?}`
//! ([`JobEnqueueParams`](protocol::JobEnqueueParams)) queues a one-off
//! background run and returns its job ([`JobInfo`](protocol::JobInfo)). A
//! tenant needs `max_queued_jobs` (`-32004`, `kind: background_runs`;
//! `-32005`, `kind: jobs`, when that many are unfinished).
//! **`job.get`** `{agent, job_id}` returns the job, its `state` one of
//! `queued`, `running`, `retrying`, `failed`, `done` and `superseded`, its
//! `run_id` once started (read it with `run.events`), or `-32043`.
//! **`job.list`** `{agent, schedule_id?, limit?}` returns `{"jobs": [...]}`
//! newest first (50 by default, at most 500). Ended jobs are kept 7 days
//! by default.
//!
//! # Server requests
//!
//! The server sends these to the client, which answers each with a
//! response carrying the same `id`. Their ids are strings `srv-<n>`, so
//! they never equal the numeric ids [`Client`] uses. Each request has a
//! timeout, which also covers waiting for room in the output queue; when
//! the server stops waiting (the timeout, a run cancel, or the call being
//! dropped) it sends the notification **`$/cancelRequest`** `{id}`
//! ([`CancelRequestParams`](protocol::CancelRequestParams)). The client may
//! then stop work and need not answer; an answer that still arrives is
//! ignored. An answer the server cannot read (malformed, or over
//! [`MAX_LINE_BYTES`]) fails the request it names: a `tool.call` with code
//! `host_protocol_error`, a `hook.call` by its `fail_mode`. When the
//! connection closes, every request still waiting fails at once.
//!
//! **`tool.call`** `{agent, name, input, session_id, run_id, tool_use_id,
//! principal?}` ([`ToolCallParams`](protocol::ToolCallParams)) runs a host
//! tool on input already checked against its schema. The result is a
//! `ToolOutput`, `{"value": <any>, "metadata"?: {...}}`; `value` is what the
//! model sees and `metadata` is for the host only (the call's
//! `tool_call_end` event carries it). An error object is a
//! tool error the model reads: its `message` is shown and its `data` kept
//! as details; when `data.code` is a string it is the error's `code`, the
//! class `spec.tool_retry` matches. Every way a call can fail without a result has a code in
//! [`tool_code`](protocol::tool_code).
//!
//! **`exec.start`**, **`exec.read`** and **`exec.kill`** run the commands of
//! an agent whose config says `"execution": {"backend": "remote"}` in the
//! client that opened it ([`ExecStartParams`](protocol::ExecStartParams),
//! [`ExecReadParams`](protocol::ExecReadParams),
//! [`ExecKillParams`](protocol::ExecKillParams)); the config is all the
//! declaration there is. `exec.start` `{agent, exec_id, command, cwd?,
//! env?, background?, session_id, principal?}` starts a command; `principal` is
//! the `principal` of the run that made the call, absent when it has none
//! (runs of several principals on one agent share the client's host, and a
//! host that serves more than one scopes or denies by it), `cwd` is the policy's
//! (the host's choice when absent) and `env` is the server's allowlisted
//! variables without its `PATH`, `HOME`, `USER`, `SHELL` and `TMPDIR`, so
//! no server secret or identity crosses. `exec.read` `{agent, exec_id,
//! wait_ms}` is a long poll: the host answers at once when the command
//! printed something or ended, else after `wait_ms`, with
//! `{"stdout"?, "stderr"?, "exit"?}`, `exit` being `{"code": n}` or
//! `{"signal": n}` and coming once, after the output it follows; the host
//! forgets the command then. `exec.kill` `{agent, exec_id}` kills the
//! command and what it started, and the host answers once it is dead with
//! the same chunk. The server enforces the timeout and the cancel: it sends
//! `exec.kill`, and reports the command as timed out or killed only when
//! the host answered with the command's `exit`. A host that stops
//! answering reads, errors, closes its connection, does not confirm a kill
//! (an answer without `exit`) or leaves a start unanswered, unreadable or
//! late (only an error answer proves the command did not start) makes the
//! call `interrupted`
//! (`[interrupted ...]`, code `interrupted`): the command's outcome is
//! unknown and none is invented. A command still running when its
//! connection closes belongs to a host that is gone, so the host must kill
//! its commands then. The commands run wherever the host runs them, with
//! the host's own isolation; the server's `execution` allowlist for a
//! tenant must list the remote entry exactly. The agent cannot run in a
//! worker or on a schedule (unless it has no `Bash`), and with a policy that does not set
//! `shared_workspace` it has no file tools.
//!
//! **`hook.call`** `{agent, point, policy_id, request}`
//! ([`HookCallParams`](protocol::HookCallParams)) asks a callback about one
//! occurrence of its point. Every `request` has `session_id` and `run_id`;
//! the rest, and the answer, depend on the point. A tool result
//! ([`WireToolResult`](protocol::WireToolResult)) is
//! `{"output": {"value", "metadata"?}}` or
//! `{"error": {"message", "code"?, "details"?}}`.
//!
//! | `point` | `request` also has | answer |
//! |---|---|---|
//! | `pre_tool_use` | `tool_use_id, tool_name, input` | `{"action": "continue"}`, `{"action": "rewrite", "input"}`, `{"action": "block", "reason"}`, `{"action": "resolve", "result"}` |
//! | `permission_request` | `tool_use_id, tool_name, input, reason?, principal?` | `{"decision": "allow"}`, `{"decision": "deny", "reason"}` |
//! | `post_tool_use` | `tool_use_id, tool_name, input, result` | `{"action": "continue"}`, `{"action": "replace", "result"}`, `{"action": "mutate", "content"?, "is_error"?}` |
//! | `tool_execute` | `tool_use_id, tool_name, input, attempt, phase, token, result?` | `phase` `before`: `{"action": "next", "after"?}` or `{"action": "veto", "result"}`; `phase` `after`: `{"action": "continue"}` or `{"action": "replace", "result"}` |
//! | `user_prompt_submit` | `content` (content blocks), and `source` set to `"steer"` only for a `run.steer` text | `{"action": "continue"}`, `{"action": "rewrite", "content"}`, `{"action": "block", "reason"}` |
//! | `context_build` | `messages` (the provider view) | `{"blocks"?: [{"text"}], "metadata"?: {..}}`; `metadata` lands on the call's `context_build` event, a later callback's key winning; the blocks count against `spec.context_budget` when set, by the callback's `policy_id`, and the event's `budget` reports what was dropped |
//! | `before_provider_call` | `turn, request` (the provider request about to be sent) | `{"action": "continue"}`, `{"action": "rewrite", "request"}`, `{"action": "force_continue", "feedback"}`, `{"action": "block", "reason"}` |
//! | `after_provider_call` | `turn, assembly` (the response just assembled) | `{"action": "continue"}`, `{"action": "rewrite", "assembly"}`, `{"action": "retry", "feedback"}` (or `"force_continue"`), `{"action": "block", "reason"}` |
//! | `stop` | `text`, `structured_output` (only with an output schema: `text` parsed and checked) | `{"action": "allow"}`, `{"action": "continue", "feedback"}` (or `"force_continue"`), `{"action": "retry", "feedback"}`, `{"action": "rewrite", "text"}`, `{"action": "block", "reason"}` |
//! | `post_turn` | `turn, stop_reason` | `{}` |
//!
//! `post_tool_use` `mutate` changes the result's `content` (the output's
//! value, or the error's message), its `is_error` flag, or both, and keeps
//! the rest (metadata, error code). `tool_execute` is Python's
//! `tools/execute` waterfall: a layer wraps one attempt of one call, the
//! first declared outermost. The `before` call asks whether to run what is
//! inside (`next`) or to veto with a result (`veto`, Python's listener that
//! does not call `next()`); `next` with `after: true` makes the server call
//! again with `phase: "after"`, the same `token` and the `result`, so the
//! layer can keep or replace it. A callback that answers `next` without
//! `after` costs one call. It runs after permission and the tool's durable
//! start, never for a call that was refused, blocked or is waiting for
//! approval. It fails closed by default: the call gets an error and the
//! tool does not run (or its result is withheld).
//!
//! A `before_provider_call` `force_continue` skips the call and sends
//! `feedback` to the model before another turn, bounded by `max_turns`
//! and by `spec.loop_guard.max_idle_turns` (25 turns in a row without a
//! call by default, always on); `retry` is refused there. An `after_provider_call` `retry`
//! keeps the response, runs none of its tool calls and sends `feedback` to
//! the model before another turn. At
//! `stop`, `continue` is bounded only by `max_turns`, and `retry` (Python's
//! `before_final_answer` retry) is honoured once per run call (with
//! `spec.verification`, while its `max_retries` lasts, each a
//! `verification` event), after which it counts as `allow`. Every other point refuses `retry` and
//! `force_continue`. With an output schema, `stop` runs only on an answer
//! that satisfies it; a `rewrite` is checked again, and one that does not
//! satisfy the schema fails the run.
//!
//! A `pre_tool_use` rewrite feeds the next callback and is validated
//! against the tool's schema again; a block or resolve stops the chain,
//! and permission still applies to a resolved call. At
//! `permission_request`, a deny stops the chain and an allow does not, so
//! a later deny wins; with no answer the call asks the host as usual
//! (`suspended`, then `run.approve`). A tool's `output_schema`, when it
//! has one, checks the value the tool returned before `post_tool_use`, so
//! a violation reaches the chain as an error result and a `replace` is
//! not checked. A blocked `user_prompt_submit` ends the run with `-32029`
//! in `run.end` before anything is written.
//!
//! # Notifications
//!
//! **`run.event`** `{run_id, envelopes}` ([`RunEvent`](protocol::RunEvent)):
//! one or more consecutive envelopes of one run, in the run's order. Each
//! is a whole [`Envelope`](linch::Envelope), `{"v": 1, "seq": 7, "run_id":
//! "...", "ts_ms": 1762400000000, "event": {"type": "...", ...}}`, that
//! decodes on its own; `ts_ms` is when the server made the envelope, in
//! Unix milliseconds. The server sends each envelope as soon as the run
//! hands it over and joins only what is already waiting to be written: the
//! envelopes queued together for one write (a turn's, say) share one
//! notification, up to about 64 KiB, and any other message or another
//! run's envelope starts a new one, so the order of the messages is
//! unchanged (a `tool.call` still follows its call's `tool_call_start`).
//! Durable kinds arrive once they are durable, so a client that saw seq
//! `n` replays with `run.events(after_seq = n)`.
//! Stream-only kinds (`partial_assistant`, `tool_progress`, `hook`) arrive
//! at once, carry the last durable seq, and are never replayed. A run's
//! envelopes end with `result` (finished) or `suspended` (waiting for
//! `run.answer` when its status is `awaiting_input`, otherwise for
//! `run.approve`).
//!
//! **`run.end`** `{run_id, error?}` ([`RunEnd`](protocol::RunEnd)): the
//! server stopped driving the run; it is the run's last notification.
//! `error` is set when the run could not continue durably (an `Envelope`
//! cannot carry that); the run then stays at its last durable state.
//! Resume or approve only after `run.end`.
//!
//! # Errors
//!
//! Error objects are `{code, message, data?}` ([`ErrorObject`]); `message`
//! includes the causes and never holds a provider key, a token or a DSN.
//!
//! | Code | Meaning | From |
//! |---|---|---|
//! | -32700 | parse error | the line is not JSON, or not UTF-8 |
//! | -32600 | invalid request | bad request object, batch, line too long, unreadable answer without an `id`, `connection.auth` after login or off the socket |
//! | -32601 | method not found | |
//! | -32602 | invalid params | unknown, missing or mistyped field; invalid host tool or callback |
//! | -32603 | internal error | a result or envelope did not encode; a request or a run (in `run.end`) panicked |
//! | -32001 | agent not found | unknown `agent` handle (handles are per connection) |
//! | -32002 | run busy | the connection already drives that run; `agent.close` of an agent with a driven run |
//! | -32003 | unauthenticated | the socket's first message is not `connection.auth` with a known token |
//! | -32004 | not allowed | a tenant check refused the request; `data: {kind}` |
//! | -32005 | limit reached | a cap on runs, agents, connections or pending logins; nothing started; `data: {kind, max}` |
//! | -32010 | missing binding | `BuildError::Missing` |
//! | -32011 | invalid agent | `BuildError::Invalid`, workspace, policy, tool name clash |
//! | -32012 | contract not digestible | `BuildError::Contract` |
//! | -32013 | provider unavailable | key unset (`data.env` names it), client build |
//! | -32014 | store unavailable | Postgres connect (or its 5 s timeout) or migrate (or its 30 s timeout) |
//! | -32015 | MCP server unavailable | first connect failed; `data.server` names it |
//! | -32020 | store error | `RunError::Store`, other store failures |
//! | -32021 | not found | `StoreError::NotFound`: no such run or session |
//! | -32022 | lease lost | `StoreError::LeaseLost`, from a claim or a journal write: another claim (another process's, or a `fail`) fenced the run out, and it stopped here without writing |
//! | -32023 | conflict | `StoreError::Conflict`, such as a stored `run_id` |
//! | -32024 | journal | `RunError::Journal` |
//! | -32025 | contract mismatch | `RunError::ContractMismatch`; `data: {expected, found}`: the run's contract, then this agent's |
//! | -32026 | terminal | `RunError::Terminal`; `data: {status}` |
//! | -32027 | not suspended | `RunError::NotSuspended`: `run.approve` on a run not awaiting approval, or `run.answer` on one not awaiting input; after a retried approve or answer, `run.resume` it |
//! | -32028 | reconcile | `RunError::Reconcile` |
//! | -32029 | prompt blocked | `RunError::PromptBlocked`; `data: {reason}` |
//! | -32030 | session busy | `StoreError::SessionBusy`: the session has a live run, so `run.start` wrote nothing; `data: {session_id, run_id}` names that run |
//! | -32031 | invalid output schema | `RunError::InvalidOutputSchema`: the `output_schema` of `run.start` does not compile or is not an object schema, so nothing was written; `data: {reason}` |
//! | -32032 | invalid answer | `RunError::InvalidAnswer`: a `run.answer` with no answers, for a request that is not pending, or that does not fit its questions; nothing was written and the run still awaits input; `data: {reason}` |
//! | -32033 | run leased | `StoreError::RunLeased`: another process drives the run and keeps its lease alive, so `run.resume`, `run.approve` or `run.answer` wrote nothing; `data: {run_id, owner, expires_in_ms}`; try again once that holder stops, at the latest after `expires_in_ms` |
//! | -32034 | invalid tool choice | `RunError::InvalidToolChoice`: the `tool_choice` of `run.start` names a tool the run does not offer, or the provider refuses it, so nothing was written; `data: {reason}` |
//! | -32035 | models unsupported | `provider.models` or `provider.model` on a provider with no model list; `data: {provider}` |
//! | -32036 | model not found | `provider.model`: the list lacks the model; `data: {model, truncated?}` |
//! | -32037 | provider request failed | the provider could not be reached or answered with an error; `data: {kind, status?}`, `kind` `unreachable`, `status`, `invalid_request` or `other`; the message is one fixed text per kind: no key, URL or vendor words |
//! | -32038 | invalid steer | `RunError::InvalidSteer`: the text of a `run.steer` is blank or over 16 KiB, its `id` is not usable, or the run is idle (queued, or running with no server driving it); nothing was written; `data: {reason}` |
//! | -32040 | not background | `schedule.create` or `job.enqueue` on an agent that cannot run in the background; `data: {reason}`: `memory_store`, `host_tools`, `callbacks` or `remote_execution` |
//! | -32041 | schedule not found | no schedule with that id in the agent's namespace |
//! | -32042 | schedule exists | the agent's namespace already has a schedule with that id |
//! | -32043 | job not found | no job with that id in the agent's namespace, or its history was pruned |
//! | -32044 | invalid schedule | the timing does not parse or never fires, an unknown time zone, an interval out of range, or both or neither of `cron` and `interval_ms`; `data: {reason}` |
//! | -32045 | workflow held | another client drives the workflow; `workflow.start` wrote nothing; `data: {run_id}` |
//! | -32046 | workflow budget | the workflow's budget is spent; `data: {spent_tokens, spent_usd, max_tokens?, max_cost_usd?}`, or `data: {kind: "unpriced", model}` when `max_cost_usd` cannot count a model with no price |
//! | -32047 | workflow call failed | a `workflow.agent` call's run failed, was aborted, timed out or waits for an approval, or its subagent is unknown; `data: {kind}` |
//! | -32048 | workflow not found | this connection drives no workflow with that id |
//! | -32049 | workflow has no journal | `workflow.start` carried `resume` answers for a workflow whose journal holds nothing (it never ran, or a memory-store agent's journal ended with its connection); nothing was claimed; `data: {run_id}` |
//!
//! Codes from -32010 to -32015 answer `agent.open`. Codes from -32020 on
//! come in `run.end` for a run and as the error of `run.events`.
//!
//! # End of input
//!
//! When the input ends (or a remote server stops), the connection stops
//! reading. Requests already read finish and are answered; an
//! `agent.open` among them still registers its agent. Every active run is
//! dropped where it stands, like a crash, so a durable (Postgres) run stays
//! resumable. Then the server requests still waiting fail, every agent's
//! MCP servers are disconnected and awaited, the queued output is written,
//! and the server returns.

#![forbid(unsafe_code)]

mod access;
mod background;
mod client;
mod error;
mod lines;
mod message;
pub mod protocol;
mod schedule_tools;
mod server;

pub use access::{
    Access, AccessError, Allowlists, ConnectionPlace, DEFAULT_MAX_AGENTS, DEFAULT_MAX_CONNECTIONS,
    DEFAULT_MAX_RUNS, DEFAULT_MAX_WORKFLOW_FAN_OUT, DEFAULT_MAX_WORKFLOW_RECORD_BYTES,
    DEFAULT_MAX_WORKFLOW_RECORDS, DEFAULT_MAX_WORKFLOWS, DEFAULT_MIN_SCHEDULE_INTERVAL,
    DEFAULT_TOOL_CACHE_BYTES, MAX_PENDING_LOGINS, McpCommand, Tenant, Tokens, WorkflowCaps,
};
pub use background::BackgroundAgents;
pub use client::{Client, ClientError, NOTIFICATION_CAPACITY, Notifications, RequestHandler};
pub use error::{ErrorObject, code};
pub use message::{Id, Message, Notification, Request, Response, Version};
pub use server::{MAX_LINE_BYTES, serve, serve_lines, serve_messages};

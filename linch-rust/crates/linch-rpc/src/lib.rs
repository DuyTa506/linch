//! The Linch JSON-RPC 2.0 protocol, with servers for byte streams
//! ([`serve`], [`serve_lines`]) and message transports
//! ([`serve_messages`]), and a [`Client`].
//!
//! A process (the `linch-server` sidecar) opens agents from configs and
//! drives their runs; the client starts, approves, cancels and replays runs
//! and receives every envelope as a notification. Everything inside a turn
//! runs in the server; it calls back into the client, on the same
//! connection, only for what the client declared: host tools, and
//! intercept callbacks at the occurrences their matchers accept.
//!
//! # Transports and access
//!
//! Each connection has its own agents, runs and server requests. Handles
//! are numbered per connection, so `agent-1` names the connection's own
//! first agent and never another connection's; `run.cancel` reaches only
//! the runs its own connection drives.
//!
//! - **stdio** ([`serve`]): `linch-server` with no arguments. The client
//!   started the server and is trusted ([`Access::Local`]): no login, no
//!   tenant, no allowlists.
//! - **Unix socket** ([`serve_lines`] with [`Access::Login`]): one message
//!   per line, as on stdio. The first message must be
//!   **`connection.auth`** `{token}` ([`AuthParams`](protocol::AuthParams)),
//!   answered with [`Authenticated`](protocol::Authenticated)
//!   `{"tenant": "acme"}`. Any other first message or an unknown token gets
//!   `-32003` and the connection closes; so does silence for 10 s. At most
//!   [`MAX_PENDING_LOGINS`] connections wait for their login at once; one
//!   more gets `-32005` (`kind: logins`) at once and closes.
//! - **HTTP** ([`serve_messages`] with [`Access::Tenant`]): `GET /rpc` with
//!   `Authorization: Bearer <token>` upgrades to a WebSocket carrying one
//!   message per text frame (a binary frame that is not UTF-8 ends the
//!   connection). A missing or unknown token gets `401` before the upgrade.
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
//! - `run.start`, `run.resume`, `run.approve`: a `principal`, if given, has
//!   the tenant's name as its `tenant`. The server does not fill one in.
//! - `agent.open`: the config passes the tenant's [`Allowlists`]:
//!   - the scripted provider only if allowed, and a client-chosen provider
//!     `base_url` (always present for vllm, sglang and llama.cpp) only at
//!     an allowed origin, since the server's key goes there;
//!   - a permission mode in the allowed list (by default `default` and
//!     `accept_edits`); `auto_allow` entries and rules that allow only
//!     with `skip_dangerous` in it, since they let calls run unasked;
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
//!     user and options;
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
//! holds its place until its `run.end`) and `max_connections` connections
//! (default [`DEFAULT_MAX_CONNECTIONS`]). Past a cap the request is
//! refused with `-32005` and `data: {kind: "runs" | "connections", max}`;
//! a refused connection gets that error (under the `connection.auth` id on
//! the socket, a null id on HTTP) and closes. A local connection has none.
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
//! handle, so resume and approve must use the same handle. Agents on one
//! Postgres DSN share one pool across all connections of a server process
//! (the [`PgPools`](linch::PgPools) given to [`serve_lines`] or
//! [`serve_messages`]); only an open that finds no pool in use connects,
//! giving up after 5 s, and runs the migrations, giving up after 30 s;
//! `agent.close` never closes a pool another agent uses. Provider keys are
//! read from the server's environment (`DEEPSEEK_API_KEY`, ...; see
//! [`ProviderConfig`](linch::ProviderConfig)) and are never params. The
//! config's `mcp_servers` are connected in order before the result is sent.
//! The result is [`AgentOpened`](protocol::AgentOpened):
//! `{"agent": "agent-1", "contract": "<hex digest>"}`. The contract covers
//! the spec, every tool registered at open (host, workspace, then MCP),
//! each with its scope, and the hook policies; a tool an MCP server adds
//! later is outside it. A
//! handle lives until `agent.close` or the end of its connection.
//!
//! Each of `tools` ([`HostToolSpec`](protocol::HostToolSpec)) is
//! `{name, description, input_schema, scope, timeout_ms?}`. `scope` is
//! required: `read`, `write` or `exec`. It decides approval like any tool's
//! (in mode `default`, `write` and `exec` calls ask), and only `read` calls
//! run in parallel. `timeout_ms` defaults to 300 000. A host tool is run
//! with `tool.call` on the connection that opened the agent.
//!
//! Each of `callbacks` ([`CallbackSpec`](protocol::CallbackSpec)) is
//! `{point, policy_id, policy_version, matcher?, timeout_ms?, fail_mode?}`
//! and is answered with `hook.call`. `point` is one of `user_prompt_submit`,
//! `context_build`, `pre_tool_use`, `permission_request`, `post_tool_use`,
//! `stop`, `post_turn`; callbacks at one point run in declaration order.
//! `matcher` is a list of tool-name globs (`*`, `?`), allowed only at the
//! three tool points; it is evaluated in the server, so an occurrence it
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
//! again. Nothing is written; the session and its runs stay, and a run of
//! it still in progress just has to read again before it writes.
//!
//! **`run.start`** `{agent, session_id, prompt, run_id?, principal?}`
//! ([`StartParams`](protocol::StartParams)) starts a run; the session is
//! created when missing. `run_id` names the new run (a fresh uuid when
//! absent), so a client can resume it even if it never saw the answer; an
//! id already stored ends the run with `-32023` and writes nothing. A
//! session has at most one run that is not terminal: a start on a session
//! whose run is still going or waiting for approval ends with `-32030`,
//! naming that run, and writes nothing. `principal` is
//! `{tenant, user, roles?}`.
//!
//! **`run.resume`** `{agent, run_id, principal?}`
//! ([`ResumeParams`](protocol::ResumeParams)) continues a run after a crash
//! or a dropped stream; a run awaiting approval suspends again.
//!
//! **`run.approve`** `{agent, run_id, decisions, principal?}`
//! ([`ApproveParams`](protocol::ApproveParams)) answers a suspended run's
//! pending calls and continues it. `decisions` maps each `tool_use_id` (from
//! the `suspended` envelope's `pending_request_ids`) to
//! `{"decision": "allow"}` or `{"decision": "deny", "reason": "..."}`.
//!
//! These three answer [`RunAccepted`](protocol::RunAccepted)
//! `{"run_id": "..."}` at once, before any notification of that run; the
//! run's outcome, errors included, then arrives as notifications. A
//! `run_id` the connection already drives is refused with `-32002`.
//!
//! **`run.cancel`** `{run_id}` ([`CancelParams`](protocol::CancelParams))
//! aborts a run the connection drives: it ends with a `result` envelope of
//! subtype `aborted`. The result is `{"cancelled": true}`, or
//! `{"cancelled": false}` when the connection does not drive that run (it
//! may have just ended); that is not an error.
//!
//! **`run.events`** `{agent, run_id, after_seq?}`
//! ([`EventsParams`](protocol::EventsParams)) returns
//! `{"events": [<envelope>...]}`: the run's durable envelopes with a seq
//! greater than `after_seq` (default 0), in order, for replay after a
//! reconnect.
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
//! model sees and `metadata` is for the host only. An error object is a
//! tool error the model reads: its `message` is shown and its `data` kept
//! as details. Every way a call can fail without a result has a code in
//! [`tool_code`](protocol::tool_code).
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
//! | `post_tool_use` | `tool_use_id, tool_name, input, result` | `{"action": "continue"}`, `{"action": "replace", "result"}` |
//! | `user_prompt_submit` | `content` (content blocks) | `{"action": "continue"}`, `{"action": "rewrite", "content"}`, `{"action": "block", "reason"}` |
//! | `context_build` | `messages` (the provider view) | `{"blocks"?: [{"text"}]}` |
//! | `stop` | `text` | `{"action": "allow"}`, `{"action": "continue", "feedback"}` |
//! | `post_turn` | `turn, stop_reason` | `{}` |
//!
//! A `pre_tool_use` rewrite feeds the next callback and is validated
//! against the tool's schema again; a block or resolve stops the chain,
//! and permission still applies to a resolved call. At
//! `permission_request`, a deny stops the chain and an allow does not, so
//! a later deny wins; with no answer the call asks the host as usual
//! (`suspended`, then `run.approve`). A blocked `user_prompt_submit` ends
//! the run with `-32029` in `run.end` before anything is written.
//!
//! # Notifications
//!
//! **`run.event`**: params are one [`Envelope`](linch::Envelope),
//! `{"v": 1, "seq": 7, "run_id": "...", "event": {"type": "...", ...}}`,
//! in the run's order. Durable kinds arrive once they are durable, so a
//! client that saw seq `n` replays with `run.events(after_seq = n)`.
//! Stream-only kinds (`partial_assistant`, `tool_progress`, `hook`) arrive
//! at once, carry the last durable seq, and are never replayed. A run's
//! envelopes end with `result` (finished) or `suspended` (waiting for
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
//! | -32005 | limit reached | a cap on runs, connections or pending logins; nothing started; `data: {kind, max}` |
//! | -32010 | missing binding | `BuildError::Missing` |
//! | -32011 | invalid agent | `BuildError::Invalid`, workspace, policy, tool name clash |
//! | -32012 | contract not digestible | `BuildError::Contract` |
//! | -32013 | provider unavailable | key unset (`data.env` names it), client build |
//! | -32014 | store unavailable | Postgres connect (or its 5 s timeout) or migrate (or its 30 s timeout) |
//! | -32015 | MCP server unavailable | first connect failed; `data.server` names it |
//! | -32020 | store error | `RunError::Store`, other store failures |
//! | -32021 | not found | `StoreError::NotFound`: no such run or session |
//! | -32022 | lease lost | `StoreError::LeaseLost` |
//! | -32023 | conflict | `StoreError::Conflict`, such as a stored `run_id` |
//! | -32024 | journal | `RunError::Journal` |
//! | -32025 | contract mismatch | `RunError::ContractMismatch`; `data: {expected, found}` |
//! | -32026 | terminal | `RunError::Terminal`; `data: {status}` |
//! | -32027 | not suspended | `RunError::NotSuspended` |
//! | -32028 | reconcile | `RunError::Reconcile` |
//! | -32029 | prompt blocked | `RunError::PromptBlocked`; `data: {reason}` |
//! | -32030 | session busy | `StoreError::SessionBusy`: the session has a live run, so `run.start` wrote nothing; `data: {session_id, run_id}` names that run |
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
mod client;
mod error;
mod lines;
mod message;
pub mod protocol;
mod server;

pub use access::{
    Access, AccessError, Allowlists, DEFAULT_MAX_CONNECTIONS, DEFAULT_MAX_RUNS, MAX_PENDING_LOGINS,
    McpCommand, Tenant, Tokens,
};
pub use client::{Client, ClientError, NOTIFICATION_CAPACITY, Notifications, RequestHandler};
pub use error::{ErrorObject, code};
pub use message::{Id, Message, Notification, Request, Response, Version};
pub use server::{MAX_LINE_BYTES, serve, serve_lines, serve_messages};

//! The Linch JSON-RPC 2.0 protocol, with a [`serve`] function for any byte
//! stream pair and a [`Client`].
//!
//! A process (the `linch-server` sidecar) opens agents from configs and
//! drives their runs; the client starts, approves, cancels and replays runs
//! and receives every envelope as a notification. Everything inside a turn
//! runs in the server; it calls back into the client, on the same
//! connection, only for what the client declared: host tools, and
//! intercept callbacks at the occurrences their matchers accept. There is
//! no socket or HTTP transport and no auth yet.
//!
//! # Framing
//!
//! One JSON-RPC message per line, UTF-8, ending in `\n`; blank lines are
//! skipped. A request line may be at most [`MAX_LINE_BYTES`] long. Batches
//! (arrays) are not supported and get one `-32600` error. `params` must be
//! an object (by-name); an absent `params` reads as `{}`. A request without
//! an `id` is a notification: the protocol defines none from client to
//! server, and JSON-RPC forbids answering one, so it is dropped silently.
//!
//! Reading is strict. A request object with a member other than
//! `jsonrpc`, `id`, `method` and `params`, a `jsonrpc` other than `"2.0"`,
//! or an `id` that is not a string, number or null is `-32600`. Params with
//! an unknown, missing or mistyped field are `-32602`. Every result is an
//! object. Calls run concurrently, so responses may arrive in any order;
//! match them by `id`.
//!
//! Both directions carry requests. The server reads a line with a `method`
//! as a request and a line with no `method` but a `result` or `error` as
//! the answer to one of its own requests; it never answers an answer. The
//! client reads each server line as exactly one of a response, a
//! notification or a request ([`Message`]).
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
//! handle, so resume and approve must use the same handle. Provider keys are
//! read from the server's environment (`DEEPSEEK_API_KEY`, ...; see
//! [`ProviderConfig`](linch::ProviderConfig)) and are never params. The
//! config's `mcp_servers` are connected in order before the result is sent
//! and disconnected when the server stops. The result is
//! [`AgentOpened`](protocol::AgentOpened):
//! `{"agent": "agent-1", "contract": "<hex digest>"}`. The contract covers
//! the spec, every tool registered at open (host, workspace, then MCP) and
//! the hook policies; a tool an MCP server adds later is outside it.
//! Handles live as long as the server.
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
//! call is blocked or denied, the result withheld, the stop refused),
//! `open` skips it. The default is `closed` at `pre_tool_use` and
//! `permission_request` and `open` elsewhere; `context_build` and
//! `post_turn` cannot fail closed. Each `(point, policy_id,
//! policy_version)` enters the contract, so bump `policy_version` whenever
//! a callback's behaviour, matcher, timeout or fail mode changes. The
//! loop runs the tool points now; the turn points register and count in
//! the contract but are not called until the loop runs them (card P6-C).
//!
//! Invalid `tools` or `callbacks` entries are `-32602`.
//!
//! **`run.start`** `{agent, session_id, prompt, principal?}`
//! ([`StartParams`](protocol::StartParams)) starts a run; the session is
//! created when missing. `principal` is `{tenant, user, roles?}`.
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
//! `run_id` the server already drives is refused with `-32002`.
//!
//! **`run.cancel`** `{run_id}` ([`CancelParams`](protocol::CancelParams))
//! aborts a run the server drives: it ends with a `result` envelope of
//! subtype `aborted`. The result is `{"cancelled": true}`, or
//! `{"cancelled": false}` when the server does not drive that run (it may
//! have just ended); that is not an error.
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
//! timeout; when the server stops waiting (the timeout, a run cancel, or
//! the call being dropped) it sends the notification
//! **`$/cancelRequest`** `{id}`
//! ([`CancelRequestParams`](protocol::CancelRequestParams)). The client may
//! then stop work and need not answer; an answer that still arrives is
//! ignored. When the connection closes, every request still waiting fails
//! at once.
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
//! (`suspended`, then `run.approve`).
//!
//! # Notifications
//!
//! **`run.event`**: params are one [`Envelope`](linch::Envelope),
//! `{"v": 1, "seq": 7, "run_id": "...", "event": {"type": "...", ...}}`,
//! in the run's order. Durable kinds arrive once they are durable, so a
//! client that saw seq `n` replays with `run.events(after_seq = n)`.
//! Stream-only kinds (`partial_assistant`, `tool_progress`) arrive at once,
//! carry the last durable seq, and are never replayed. A run's envelopes
//! end with `result` (finished) or `suspended` (waiting for `run.approve`).
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
//! includes the causes and never holds a provider key or a DSN.
//!
//! | Code | Meaning | From |
//! |---|---|---|
//! | -32700 | parse error | the line is not JSON |
//! | -32600 | invalid request | bad request object, batch, line too long, malformed answer |
//! | -32601 | method not found | |
//! | -32602 | invalid params | unknown, missing or mistyped field; invalid host tool or callback |
//! | -32603 | internal error | a result or envelope did not encode |
//! | -32001 | agent not found | unknown `agent` handle |
//! | -32002 | run busy | the server already drives that run |
//! | -32010 | missing binding | `BuildError::Missing` |
//! | -32011 | invalid agent | `BuildError::Invalid`, workspace, policy, tool name clash |
//! | -32012 | contract not digestible | `BuildError::Contract` |
//! | -32013 | provider unavailable | key unset (`data.env` names it), client build |
//! | -32014 | store unavailable | Postgres connect or migrate |
//! | -32015 | MCP server unavailable | first connect failed; `data.server` names it |
//! | -32020 | store error | `RunError::Store`, other store failures |
//! | -32021 | not found | `StoreError::NotFound`: no such run or session |
//! | -32022 | lease lost | `StoreError::LeaseLost` |
//! | -32023 | conflict | `StoreError::Conflict` |
//! | -32024 | journal | `RunError::Journal` |
//! | -32025 | contract mismatch | `RunError::ContractMismatch`; `data: {expected, found}` |
//! | -32026 | terminal | `RunError::Terminal`; `data: {status}` |
//! | -32027 | not suspended | `RunError::NotSuspended` |
//! | -32028 | reconcile | `RunError::Reconcile` |
//! | -32029 | prompt blocked | `RunError::PromptBlocked`; `data: {reason}` |
//!
//! Codes from -32010 to -32015 answer `agent.open`. Codes from -32020 on
//! come in `run.end` for a run and as the error of `run.events`. A
//! malformed answer to a server request with a readable `id` fails that
//! request with `-32600` instead of being answered.
//!
//! # End of input
//!
//! When the input ends, [`serve`] drops every active run where it stands,
//! like a crash, so a durable (Postgres) run stays resumable; it fails the
//! server requests still waiting, disconnects every agent's MCP servers,
//! writes the queued output and returns.

#![forbid(unsafe_code)]

mod client;
mod error;
mod message;
pub mod protocol;
mod server;

pub use client::{Client, ClientError, Notifications, RequestHandler};
pub use error::{ErrorObject, code};
pub use message::{Id, Message, Notification, Request, Response, Version};
pub use server::{MAX_LINE_BYTES, serve};

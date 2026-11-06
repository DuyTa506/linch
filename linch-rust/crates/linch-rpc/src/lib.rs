//! The Linch JSON-RPC 2.0 protocol, with a [`serve`] function for any byte
//! stream pair and a [`Client`].
//!
//! A process (the `linch-server` sidecar) opens agents from configs and
//! drives their runs; the client starts, approves, cancels and replays runs
//! and receives every envelope as a notification. Everything inside a turn
//! runs in the server. This first slice has no host tools, no intercept
//! callbacks, no socket or HTTP transport and no auth.
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
//! # Methods
//!
//! **`agent.open`**: params are an [`AgentConfig`](linch::AgentConfig):
//!
//! ```json
//! {"namespace": "acme",
//!  "spec": {"model": "deepseek-flash", "system_prompt": "Be brief."},
//!  "provider": {"id": "deepseek"},
//!  "stores": {"type": "postgres", "dsn": "postgres://linch@db/linch"},
//!  "workspace": "/srv/acme",
//!  "permissions": {"mode": "accept_edits", "project_root": "/srv/acme"}}
//! ```
//!
//! `stores` defaults to `{"type": "memory"}`: stores owned by this agent
//! handle, so resume and approve must use the same handle. Provider keys are
//! read from the server's environment (`DEEPSEEK_API_KEY`, ...; see
//! [`ProviderConfig`](linch::ProviderConfig)) and are never params. The
//! result is [`AgentOpened`](protocol::AgentOpened):
//! `{"agent": "agent-1", "contract": "<hex digest>"}`. Handles live as long
//! as the server.
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
//! | -32600 | invalid request | bad request object, batch, line too long |
//! | -32601 | method not found | |
//! | -32602 | invalid params | unknown, missing or mistyped field |
//! | -32603 | internal error | a result or envelope did not encode |
//! | -32001 | agent not found | unknown `agent` handle |
//! | -32002 | run busy | the server already drives that run |
//! | -32010 | missing binding | `BuildError::Missing` |
//! | -32011 | invalid agent | `BuildError::Invalid`, workspace, policy, tools |
//! | -32012 | contract not digestible | `BuildError::Contract` |
//! | -32013 | provider unavailable | key unset (`data.env` names it), client build |
//! | -32014 | store unavailable | Postgres connect or migrate |
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
//! Codes from -32010 to -32014 answer `agent.open`. Codes from -32020 on
//! come in `run.end` for a run and as the error of `run.events`.
//!
//! # End of input
//!
//! When the input ends, [`serve`] drops every active run where it stands,
//! like a crash, so a durable (Postgres) run stays resumable; it writes the
//! queued output and returns.

#![forbid(unsafe_code)]

mod client;
mod error;
mod message;
pub mod protocol;
mod server;

pub use client::{Client, ClientError, Notifications};
pub use error::{ErrorObject, code};
pub use message::{Id, Message, Notification, Request, Response, Version};
pub use server::{MAX_LINE_BYTES, serve};

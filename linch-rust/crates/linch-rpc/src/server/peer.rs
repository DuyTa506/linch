//! Requests from the server to the client on the same connection.
//!
//! Each request gets a string id `srv-<n>`, so it never equals one of the
//! client's numeric ids. A call that stops waiting before its answer (a
//! timeout, a cancel, or its future being dropped) forgets the id and sends
//! `$/cancelRequest`; an answer that comes later finds no waiter and is
//! dropped. Closing the peer fails every waiting call at once.

use std::collections::HashMap;
use std::future::pending;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use linch::{JsonObject, JsonValue};
use tokio::sync::oneshot;
use tokio_util::sync::CancellationToken;

use super::{Outbox, lock};
use crate::error::ErrorObject;
use crate::message::{Id, Notification, Request, Response, Version};
use crate::protocol::{CancelRequestParams, method};

/// Prefix of every server request id.
const ID_PREFIX: &str = "srv-";

/// Why a server request produced no result.
#[derive(Debug, thiserror::Error)]
pub(crate) enum CallError {
    /// The client answered with an error object.
    #[error("the client answered with an error: {0}")]
    Rpc(ErrorObject),
    /// No answer within the request's timeout.
    #[error("no answer within {} ms", .0.as_millis())]
    Timeout(Duration),
    /// The caller's cancel token fired.
    #[error("the request was cancelled")]
    Cancelled,
    /// The connection is closed, or closed while waiting.
    #[error("the host connection is closed")]
    Closed,
}

/// The server's side of its requests to the client. Cheap to clone.
#[derive(Clone)]
pub(crate) struct Peer(Arc<Inner>);

struct Inner {
    outbox: Outbox,
    next_id: AtomicU64,
    calls: Mutex<Calls>,
}

#[derive(Default)]
struct Calls {
    closed: bool,
    waiting: HashMap<String, oneshot::Sender<Response>>,
}

/// Forgets a call that ends without its answer and tells the client.
struct Waiting<'a> {
    peer: &'a Inner,
    id: String,
}

impl Peer {
    pub(crate) fn new(outbox: Outbox) -> Self {
        Self(Arc::new(Inner {
            outbox,
            next_id: AtomicU64::new(0),
            calls: Mutex::default(),
        }))
    }

    /// Send `method` to the client and wait for its result.
    ///
    /// Stops waiting after `timeout`, when `cancel` fires, or when the
    /// future is dropped; the client then gets `$/cancelRequest`.
    pub(crate) async fn call(
        &self,
        method: &str,
        params: JsonObject,
        timeout: Option<Duration>,
        cancel: Option<&CancellationToken>,
    ) -> Result<JsonValue, CallError> {
        let number = self.0.next_id.fetch_add(1, Ordering::Relaxed) + 1;
        let id = format!("{ID_PREFIX}{number}");
        let (answer, answered) = oneshot::channel();
        {
            let mut calls = lock(&self.0.calls);
            if calls.closed {
                return Err(CallError::Closed);
            }
            calls.waiting.insert(id.clone(), answer);
        }
        let _waiting = Waiting {
            peer: &self.0,
            id: id.clone(),
        };
        let request = Request {
            jsonrpc: Version::V2,
            id: Some(Id::String(id)),
            method: method.into(),
            params,
        };
        if !self.0.outbox.send(&request).await {
            return Err(CallError::Closed);
        }
        let response = tokio::select! {
            biased;
            response = answered => response.map_err(|_| CallError::Closed)?,
            () = fired(cancel) => return Err(CallError::Cancelled),
            () = elapsed(timeout) => {
                return Err(CallError::Timeout(timeout.unwrap_or_default()));
            }
        };
        response.into_result().map_err(CallError::Rpc)
    }

    /// Hand a client's response to the call waiting for it. A response to
    /// an id nobody waits for (late, after a cancel) is dropped.
    pub(crate) fn answer(&self, response: Response) {
        let Id::String(id) = &response.id else { return };
        let waiter = lock(&self.0.calls).waiting.remove(id);
        if let Some(waiter) = waiter {
            let _ = waiter.send(response);
        }
    }

    /// The connection closed: fail every waiting call with
    /// [`CallError::Closed`] now, and every later one at once.
    pub(crate) fn close(&self) {
        let mut calls = lock(&self.0.calls);
        calls.closed = true;
        calls.waiting.clear();
    }
}

impl Drop for Waiting<'_> {
    fn drop(&mut self) {
        // Absent once answered or closed; present when the caller gave up.
        let gave_up = lock(&self.peer.calls).waiting.remove(&self.id).is_some();
        if gave_up {
            let params = CancelRequestParams {
                id: Id::String(std::mem::take(&mut self.id)),
            };
            let params = serde_json::to_value(params).expect("an id encodes");
            let cancel = Notification::new(method::CANCEL_REQUEST, params);
            self.peer.outbox.send_now(&cancel);
        }
    }
}

async fn fired(cancel: Option<&CancellationToken>) {
    match cancel {
        Some(token) => token.cancelled().await,
        None => pending().await,
    }
}

async fn elapsed(timeout: Option<Duration>) {
    match timeout {
        Some(limit) => tokio::time::sleep(limit).await,
        None => pending().await,
    }
}

#[cfg(test)]
mod tests;

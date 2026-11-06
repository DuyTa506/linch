//! The socket login: the first message must be `connection.auth` with a
//! known token. Anything else, or nothing in time, ends the connection.

use std::sync::Arc;
use std::time::Duration;

use futures::{Stream, StreamExt};
use linch::JsonValue;
use tokio::sync::OwnedSemaphorePermit;
use tokio_util::sync::CancellationToken;

use super::connection::{Inbound, Outbox};
use crate::access::{Tenant, Tokens};
use crate::error::{ErrorObject, code};
use crate::message::{Id, Request, Response};
use crate::protocol::{AuthParams, Authenticated, method};

/// How long a new connection may take to send `connection.auth`.
const LOGIN_TIMEOUT: Duration = Duration::from_secs(10);

/// Read and answer the first message: the tenant, with the connection's
/// place among the tenant's connections. `None` when the login was
/// refused (answered with `-32003`, or `-32005` past the tenant's
/// connection cap), timed out, or `stop` fired.
pub(super) async fn login<S>(
    input: &mut S,
    tokens: &Tokens,
    outbox: &Outbox,
    stop: &CancellationToken,
) -> Option<(Arc<Tenant>, OwnedSemaphorePermit)>
where
    S: Stream<Item = Inbound> + Unpin,
{
    let first = tokio::select! {
        biased;
        () = stop.cancelled() => return None,
        first = tokio::time::timeout(LOGIN_TIMEOUT, input.next()) => first.ok().flatten()?,
    };
    let request = match first {
        Inbound::Message(message) => serde_json::from_str::<JsonValue>(&message)
            .ok()
            .and_then(|value| Request::from_value(value).ok()),
        Inbound::TooLong(_) | Inbound::NotUtf8 => None,
        Inbound::Failed(_) => return None,
    };
    let id = (request.as_ref())
        .and_then(|request| request.id.clone())
        .unwrap_or(Id::Null);
    // The token is never part of an answer, and the refusal says nothing
    // about why, so a wrong token and a malformed request look the same.
    let tenant = (request.filter(|request| request.method == method::CONNECTION_AUTH))
        .and_then(|request| {
            serde_json::from_value::<AuthParams>(JsonValue::Object(request.params)).ok()
        })
        .and_then(|params| tokens.tenant(&params.token));
    let admitted = tenant.map(|tenant| tenant.connection_place().map(|place| (tenant, place)));
    match admitted {
        Some(Ok((tenant, place))) => {
            let result = Authenticated {
                tenant: tenant.name().to_owned(),
            };
            let result = serde_json::to_value(result).expect("a tenant name encodes");
            outbox.send(&Response::ok(id, result)).await;
            Some((tenant, place))
        }
        Some(Err(error)) => {
            outbox.send(&Response::err(id, error)).await;
            None
        }
        None => {
            let message = "authentication required: the first message must be \
                           connection.auth with a valid token";
            let error = ErrorObject::new(code::UNAUTHENTICATED, message);
            outbox.send(&Response::err(id, error)).await;
            None
        }
    }
}

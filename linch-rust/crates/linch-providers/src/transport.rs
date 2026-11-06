//! HTTP mechanics every adapter shares.
//!
//! - [`client`]: the one way an adapter builds its HTTP client. It never
//!   follows a redirect, because reqwest only strips `Authorization` on a
//!   cross-origin hop; `x-api-key` and `x-goog-api-key` would go along.
//! - [`error_body`]: the start of a non-2xx body, never the whole of it.

use std::time::Duration;

use linch_core::ProviderError;
use reqwest::redirect::Policy;
use tokio_util::sync::CancellationToken;

/// Most bytes of an error body read. Error summaries keep 500 characters,
/// and vendor error JSON fits well inside this.
const MAX_ERROR_BODY: usize = 8 * 1024;

/// The message of the error a redirect answer becomes.
pub(crate) const REDIRECT_REFUSED: &str =
    "redirects are not followed, so credentials never leave the configured base URL";

/// An HTTP client with these timeouts that never follows a redirect.
pub(crate) fn client(connect: Duration, read: Duration) -> reqwest::Result<reqwest::Client> {
    reqwest::Client::builder()
        .connect_timeout(connect)
        .read_timeout(read)
        .redirect(Policy::none())
        .build()
}

/// The first [`MAX_ERROR_BODY`] bytes of `response`'s body as text, so a
/// huge or endless error body cannot hold the call or its memory. A read
/// error ends the body early.
pub(crate) async fn error_body(
    mut response: reqwest::Response,
    cancel: &CancellationToken,
) -> Result<String, ProviderError> {
    let read = async {
        let mut body = Vec::new();
        while body.len() < MAX_ERROR_BODY {
            let Ok(Some(chunk)) = response.chunk().await else {
                break;
            };
            let room = MAX_ERROR_BODY - body.len();
            body.extend_from_slice(&chunk[..chunk.len().min(room)]);
        }
        // A cut can split a character; it decodes as U+FFFD.
        String::from_utf8_lossy(&body).into_owned()
    };
    cancel
        .run_until_cancelled(read)
        .await
        .ok_or(ProviderError::Cancelled)
}

/// The error for a `3xx` answer: a status, so it is not retried.
pub(crate) fn redirect_refused(status: u16) -> ProviderError {
    ProviderError::Status {
        status,
        retry_after: None,
        message: REDIRECT_REFUSED.into(),
    }
}

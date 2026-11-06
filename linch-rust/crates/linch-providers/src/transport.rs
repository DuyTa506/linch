//! HTTP mechanics every adapter shares.
//!
//! - [`client`]: the one way an adapter builds its HTTP client. It never
//!   follows a redirect, because reqwest only strips `Authorization` on a
//!   cross-origin hop; `x-api-key` and `x-goog-api-key` would go along.

use std::time::Duration;

use linch_core::ProviderError;
use reqwest::redirect::Policy;

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

/// The error for a `3xx` answer: a status, so it is not retried.
pub(crate) fn redirect_refused(status: u16) -> ProviderError {
    ProviderError::Status {
        status,
        retry_after: None,
        message: REDIRECT_REFUSED.into(),
    }
}

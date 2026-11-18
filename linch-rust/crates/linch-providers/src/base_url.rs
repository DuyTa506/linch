//! The shape a provider's `base_url` must have.
//!
//! An adapter builds each request URL by appending a path to the base
//! (`{base}/models`, `{base}/v1/models?limit=1000`, `{base}/chat/completions`),
//! and sends its key there. A base that carries a query or a fragment turns
//! that append into a different request (`https://host/v1/files#` makes
//! `GET /v1/files#/models` a `GET /v1/files`), and a base with a dot
//! segment, an escape or userinfo reaches other paths or hosts than it
//! reads as. [`check_base_url`] refuses all of these.
//!
//! The check reads the text as it was given: [`reqwest::Url`] resolves dot
//! segments and rewrites backslashes while it parses, so the parsed form
//! alone never shows them. It decides the shape only; which paths a tenant
//! may use is the host's allowlist.

use linch_core::ProviderError;
use reqwest::Url;
use thiserror::Error;

/// Why a base URL is refused. No message holds the URL.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum BaseUrlError {
    /// Not an absolute `http` or `https` URL with a host.
    #[error("the base URL is not an absolute http or https URL")]
    NotHttp,
    /// It names a user or a password.
    #[error("the base URL must not carry userinfo")]
    Userinfo,
    /// It carries a query or a fragment.
    #[error("the base URL must not carry a query or a fragment")]
    QueryOrFragment,
    /// A percent escape, a backslash, a space or a control character.
    #[error("the base URL must not carry escapes, backslashes, spaces or control characters")]
    Escaped,
    /// A `.` or `..` segment, or an empty one (`//`) before the last.
    #[error("the base URL's path must not have dot or empty segments")]
    PathSegments,
}

/// Refuse a base URL that could steer a request off its path or host: see
/// the module docs. A path such as `/v1` or `/openai/v1/` is fine.
///
/// Args:
///     raw: The base URL as configured.
///
/// Returns:
///     `Ok` when the URL is an absolute `http` or `https` URL whose path is
///     plain segments.
///
/// Raises:
///     BaseUrlError: As the variant says.
pub fn check_base_url(raw: &str) -> Result<(), BaseUrlError> {
    if raw.contains(['?', '#']) {
        return Err(BaseUrlError::QueryOrFragment);
    }
    if raw
        .chars()
        .any(|c| c == '%' || c == '\\' || c.is_whitespace() || c.is_control())
    {
        return Err(BaseUrlError::Escaped);
    }
    let url = Url::parse(raw).map_err(|_| BaseUrlError::NotHttp)?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
        return Err(BaseUrlError::NotHttp);
    }
    // `scheme://authority/path`: anything else (`http:host`) is not plain.
    let after = raw
        .split_once("://")
        .map(|(_, rest)| rest)
        .ok_or(BaseUrlError::NotHttp)?;
    let (authority, path) = match after.find('/') {
        Some(at) => after.split_at(at),
        None => (after, ""),
    };
    if authority.contains('@') || !url.username().is_empty() || url.password().is_some() {
        return Err(BaseUrlError::Userinfo);
    }
    let plain = |segment: &str| !matches!(segment, "." | "..");
    let mut segments = path.split('/').skip(1).peekable();
    while let Some(segment) = segments.next() {
        let last = segments.peek().is_none();
        if !plain(segment) || (segment.is_empty() && !last) {
            return Err(BaseUrlError::PathSegments);
        }
    }
    // What the parser made of the path is what was written.
    if path != url.path() && !(path.is_empty() && url.path() == "/") {
        return Err(BaseUrlError::PathSegments);
    }
    Ok(())
}

/// [`check_base_url`] as the error an adapter's constructor returns.
pub(crate) fn require(raw: &str) -> Result<(), ProviderError> {
    check_base_url(raw).map_err(|error| ProviderError::InvalidRequest {
        message: error.to_string(),
    })
}

#[cfg(test)]
mod tests;

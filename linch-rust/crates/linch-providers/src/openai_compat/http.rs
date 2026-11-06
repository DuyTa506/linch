//! The streaming POST and HTTP failure mapping.

use std::time::Duration;

use linch_core::{JsonValue, ProviderError};
use reqwest::header::{ACCEPT, RETRY_AFTER};
use tokio_util::sync::CancellationToken;

use crate::ApiKey;
use crate::api_key::scrub;

/// Longest error-body summary kept in a [`ProviderError::Status`].
const MAX_ERROR_CHARS: usize = 500;

/// POST `body` to `url` and return the response once its status is 2xx.
///
/// `key`, when given, is sent as a bearer token. A redirect is refused,
/// never followed; any other non-2xx status reads the start of the body and
/// maps it with [`status_error`].
pub(crate) async fn post(
    client: &reqwest::Client,
    url: &str,
    key: Option<&ApiKey>,
    body: &JsonValue,
    cancel: &CancellationToken,
) -> Result<reqwest::Response, ProviderError> {
    let mut request = client
        .post(url)
        .header(ACCEPT, "text/event-stream")
        .json(body);
    if let Some(key) = key {
        request = request.bearer_auth(key.expose());
    }
    let request = request.send();
    let response = cancel
        .run_until_cancelled(request)
        .await
        .ok_or(ProviderError::Cancelled)?
        .map_err(|err| transport(key, &err))?;
    let status = response.status();
    if status.is_success() {
        return Ok(response);
    }
    if status.is_redirection() {
        return Err(crate::transport::redirect_refused(status.as_u16()));
    }
    let retry_after = response
        .headers()
        .get(RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .map(String::from);
    let text = crate::transport::error_body(response, cancel).await?;
    Err(status_error(
        status.as_u16(),
        retry_after.as_deref(),
        &text,
        key,
    ))
}

/// A request that got no response, with its text redacted.
pub(crate) fn transport(key: Option<&ApiKey>, err: &reqwest::Error) -> ProviderError {
    ProviderError::Transport {
        message: scrub(key, &err.to_string()),
    }
}

/// Map a non-2xx response to [`ProviderError::Status`].
///
/// The summary prefers the OpenAI-style `error.message`; it is redacted
/// before it is cut, so a cut never leaves part of a key behind.
/// `Retry-After` is read as whole seconds; an HTTP date is ignored.
pub(crate) fn status_error(
    status: u16,
    retry_after: Option<&str>,
    body: &str,
    key: Option<&ApiKey>,
) -> ProviderError {
    let summary = serde_json::from_str::<JsonValue>(body)
        .ok()
        .and_then(|value| {
            let error = value.get("error")?;
            let message = error.get("message").and_then(JsonValue::as_str)?;
            Some(match error.get("type").and_then(JsonValue::as_str) {
                Some(kind) => format!("{kind}: {message}"),
                None => message.to_string(),
            })
        })
        .unwrap_or_else(|| body.trim().to_string());
    let message: String = scrub(key, &summary).chars().take(MAX_ERROR_CHARS).collect();
    ProviderError::Status {
        status,
        retry_after: retry_after
            .and_then(|value| value.trim().parse::<u64>().ok())
            .map(Duration::from_secs),
        message,
    }
}

/// The HTTP status for an error sent inside a 200 stream, so it retries
/// like the same error outside a stream: `code` when it is an HTTP error
/// status, else the status OpenAI answers with for that string `code`,
/// else for the error `type`. `None` when none of them is known.
pub(crate) fn stream_status(code: Option<&JsonValue>, kind: Option<&str>) -> Option<u16> {
    code.and_then(JsonValue::as_u64)
        .and_then(|code| u16::try_from(code).ok())
        .filter(|code| (400..=599).contains(code))
        .or_else(|| code.and_then(JsonValue::as_str).and_then(known_status))
        .or_else(|| kind.and_then(known_status))
}

/// OpenAI's error codes and types, including the Responses
/// `response.error.code` values, by the HTTP status they come with.
fn known_status(name: &str) -> Option<u16> {
    Some(match name {
        "invalid_request_error"
        | "context_length_exceeded"
        | "invalid_prompt"
        | "invalid_image"
        | "invalid_image_format"
        | "invalid_base64_image"
        | "invalid_image_url"
        | "invalid_image_mode"
        | "image_too_large"
        | "image_too_small"
        | "image_parse_error"
        | "image_content_policy_violation"
        | "image_file_too_large"
        | "image_file_not_found"
        | "unsupported_image_media_type"
        | "empty_image_file"
        | "failed_to_download_image" => 400,
        "invalid_api_key" | "authentication_error" => 401,
        "model_not_found" => 404,
        // Quota runs out with a 429 over HTTP too, so it retries alike.
        "rate_limit_exceeded" | "insufficient_quota" => 429,
        "server_error" => 500,
        "vector_store_timeout" => 504,
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &str = "sk-live-0123456789abcdef";

    fn key() -> ApiKey {
        ApiKey::new(SECRET).unwrap()
    }

    fn assert_clean(err: &ProviderError) {
        for text in [err.to_string(), format!("{err:?}")] {
            assert!(!text.contains(SECRET), "{text}");
            assert!(!text.contains("0123456789abcdef"), "{text}");
        }
    }

    #[test]
    fn error_message_and_type_are_summarized() {
        let body = r#"{"error":{"message":"Insufficient Balance","type":"unknown_error","param":null,"code":"invalid_request_error"}}"#;
        let err = status_error(402, None, body, Some(&key()));
        assert!(
            matches!(&err, ProviderError::Status { status: 402, retry_after: None, message }
                if message == "unknown_error: Insufficient Balance"),
            "{err:?}"
        );
        assert!(!err.retryable());
    }

    #[test]
    fn rate_limit_keeps_retry_after_seconds() {
        let err = status_error(429, Some(" 7 "), "{}", Some(&key()));
        assert!(
            matches!(&err, ProviderError::Status { status: 429, retry_after: Some(delay), .. }
                if *delay == Duration::from_secs(7)),
            "{err:?}"
        );
        assert!(err.retryable());
        let dated = status_error(429, Some("Wed, 21 Oct 2015 07:28:00 GMT"), "", Some(&key()));
        assert!(matches!(
            dated,
            ProviderError::Status {
                retry_after: None,
                ..
            }
        ));
    }

    #[test]
    fn unauthorized_body_echoing_the_header_is_redacted() {
        let body = format!(
            r#"{{"error":{{"message":"Authentication Fails, header Authorization: Bearer {SECRET} is invalid","type":"authentication_error"}}}}"#
        );
        let err = status_error(401, None, &body, Some(&key()));
        assert_clean(&err);
        assert!(err.to_string().contains("Bearer [redacted]"), "{err}");
    }

    #[test]
    fn rate_limit_plain_body_echoing_headers_is_redacted() {
        let body = format!(
            "429 Too Many Requests\nrequest headers: authorization=Bearer {SECRET}; x-api-key={SECRET}"
        );
        let err = status_error(429, Some("2"), &body, Some(&key()));
        assert_clean(&err);
    }

    #[test]
    fn a_long_body_is_redacted_before_it_is_cut() {
        let body = format!("{}{SECRET}", "x".repeat(MAX_ERROR_CHARS - 5));
        let err = status_error(500, None, &body, Some(&key()));
        assert_clean(&err);
        assert!(
            matches!(&err, ProviderError::Status { message, .. } if message.chars().count() == MAX_ERROR_CHARS)
        );
    }
}

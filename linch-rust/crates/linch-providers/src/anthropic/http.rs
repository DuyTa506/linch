//! The Messages POST and the mapping of HTTP and stream errors.

use std::time::Duration;

use linch_core::{JsonValue, ProviderError};
use reqwest::header::{ACCEPT, RETRY_AFTER};
use tokio_util::sync::CancellationToken;

use super::API_VERSION;
use crate::ApiKey;

/// Longest error summary kept in a [`ProviderError`].
const MAX_ERROR_CHARS: usize = 500;

/// POST `body` to `url` with `x-api-key` auth. A redirect is refused,
/// never followed; any other non-2xx maps through [`status_error`].
pub(crate) async fn post(
    client: &reqwest::Client,
    url: &str,
    key: &ApiKey,
    body: &JsonValue,
    cancel: &CancellationToken,
) -> Result<reqwest::Response, ProviderError> {
    let request = client
        .post(url)
        .header("x-api-key", key.expose())
        .header("anthropic-version", API_VERSION)
        .header(ACCEPT, "text/event-stream")
        .json(body)
        .send();
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
pub(crate) fn transport(key: &ApiKey, err: &reqwest::Error) -> ProviderError {
    ProviderError::Transport {
        message: key.redact(&err.to_string()),
    }
}

/// Map a non-2xx response to [`ProviderError::Status`].
///
/// The body is `{"type": "error", "error": {"type", "message"}}`; the
/// summary is `type: message`, or the trimmed body when it is not that
/// shape. `Retry-After` is read as whole seconds.
pub(crate) fn status_error(
    status: u16,
    retry_after: Option<&str>,
    body: &str,
    key: &ApiKey,
) -> ProviderError {
    let summary = serde_json::from_str::<JsonValue>(body)
        .ok()
        .and_then(|value| summarize(value.get("error")?))
        .unwrap_or_else(|| body.trim().to_string());
    ProviderError::Status {
        status,
        retry_after: retry_after
            .and_then(|value| value.trim().parse::<u64>().ok())
            .map(Duration::from_secs),
        message: clean(&summary, key),
    }
}

/// An `error` object received inside a 200 stream.
///
/// Its `type` maps to the HTTP status the same error has outside a
/// stream, so [`ProviderError::retryable`] treats both alike; an unknown
/// type is a retryable transport error.
pub(crate) fn stream_error(error: &JsonValue, key: &ApiKey) -> ProviderError {
    let summary = summarize(error).unwrap_or_else(|| error.to_string());
    let message = clean(&summary, key);
    let status = match error.get("type").and_then(JsonValue::as_str) {
        Some("invalid_request_error") => 400,
        Some("authentication_error") => 401,
        Some("billing_error") => 402,
        Some("permission_error") => 403,
        Some("not_found_error") => 404,
        Some("request_too_large") => 413,
        Some("rate_limit_error") => 429,
        Some("api_error") => 500,
        Some("timeout_error") => 504,
        Some("overloaded_error") => 529,
        _ => return ProviderError::Transport { message },
    };
    ProviderError::Status {
        status,
        retry_after: None,
        message,
    }
}

fn summarize(error: &JsonValue) -> Option<String> {
    let message = error.get("message").and_then(JsonValue::as_str)?;
    Some(match error.get("type").and_then(JsonValue::as_str) {
        Some(kind) => format!("{kind}: {message}"),
        None => message.to_string(),
    })
}

/// Redact first, then cut, so a cut never leaves part of a key behind.
fn clean(summary: &str, key: &ApiKey) -> String {
    key.redact(summary).chars().take(MAX_ERROR_CHARS).collect()
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    const SECRET: &str = "sk-ant-api03-unit-test-secret";

    fn key() -> ApiKey {
        ApiKey::new(SECRET).unwrap()
    }

    fn assert_clean(err: &ProviderError) {
        for text in [err.to_string(), format!("{err:?}")] {
            assert!(!text.contains(SECRET), "{text}");
        }
    }

    #[test]
    fn error_body_is_summarized_as_type_and_message() {
        let body = r#"{"type":"error","error":{"type":"not_found_error","message":"model: nope"},"request_id":"req_1"}"#;
        let err = status_error(404, None, body, &key());
        assert!(
            matches!(&err, ProviderError::Status { status: 404, retry_after: None, message }
                if message == "not_found_error: model: nope"),
            "{err:?}"
        );
        assert!(!err.retryable());
    }

    #[test]
    fn rate_limit_keeps_retry_after_seconds() {
        let err = status_error(429, Some("12"), "{}", &key());
        assert!(
            matches!(&err, ProviderError::Status { status: 429, retry_after: Some(delay), .. }
                if *delay == Duration::from_secs(12)),
            "{err:?}"
        );
        assert!(err.retryable());
    }

    #[test]
    fn a_body_echoing_the_api_key_header_is_redacted() {
        let body = format!(
            r#"{{"type":"error","error":{{"type":"authentication_error","message":"bad header x-api-key: {SECRET}"}}}}"#
        );
        let err = status_error(401, None, &body, &key());
        assert_clean(&err);
        let plain = format!("{}x-api-key={SECRET}", "y".repeat(MAX_ERROR_CHARS - 12));
        assert_clean(&status_error(500, None, &plain, &key()));
    }

    #[test]
    fn stream_error_types_map_to_their_http_status() {
        for (kind, status, retryable) in [
            ("overloaded_error", 529, true),
            ("api_error", 500, true),
            ("rate_limit_error", 429, true),
            ("invalid_request_error", 400, false),
            ("authentication_error", 401, false),
        ] {
            let err = stream_error(&json!({"type": kind, "message": "m"}), &key());
            assert!(
                matches!(&err, ProviderError::Status { status: s, .. } if *s == status),
                "{err:?}"
            );
            assert_eq!(err.retryable(), retryable, "{kind}");
        }
        let unknown = stream_error(&json!({"type": "new_error", "message": "m"}), &key());
        assert!(matches!(unknown, ProviderError::Transport { .. }));
        let echoed = stream_error(&json!({"message": format!("key {SECRET}")}), &key());
        assert_clean(&echoed);
    }
}

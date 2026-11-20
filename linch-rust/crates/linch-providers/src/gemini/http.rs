//! The `streamGenerateContent` POST and the mapping of Google API errors.

use std::time::Duration;

use linch_core::{JsonValue, ProviderError};
use reqwest::header::{ACCEPT, RETRY_AFTER};
use tokio_util::sync::CancellationToken;

use crate::ApiKey;
use crate::overflow;

const RETRY_INFO: &str = "type.googleapis.com/google.rpc.RetryInfo";

/// POST `body` to `url` with `x-goog-api-key` auth. A redirect is refused,
/// never followed; any other non-2xx maps through [`status_error`]. The key
/// never goes into the URL, where transport errors would quote it.
pub(crate) async fn post(
    client: &reqwest::Client,
    url: &str,
    key: &ApiKey,
    send_key: bool,
    body: &JsonValue,
    cancel: &CancellationToken,
) -> Result<reqwest::Response, ProviderError> {
    let mut request = client.post(url).header(ACCEPT, "text/event-stream");
    if send_key {
        request = request.header("x-goog-api-key", key.expose());
    }
    let request = request.json(body).send();
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

/// A request that got no response, with its text redacted: a
/// [`ProviderError::Connect`] when the connection was never made, so the
/// request never went out.
pub(crate) fn transport(key: &ApiKey, err: &reqwest::Error) -> ProviderError {
    let message = key.redact(&err.to_string());
    if err.is_connect() {
        ProviderError::Connect { message }
    } else {
        ProviderError::Transport { message }
    }
}

/// Map a non-2xx response to [`ProviderError::Status`], or to
/// [`ProviderError::ContextLength`] for a prompt too long for the model
/// (see [`overflow::GEMINI`]).
///
/// The body is `{"error": {"code", "message", "status", "details"}}`; the
/// summary is `status: message`. The delay comes from `Retry-After`
/// (whole seconds), else from a `RetryInfo` detail's `retryDelay`.
pub(crate) fn status_error(
    status: u16,
    retry_after: Option<&str>,
    body: &str,
    key: &ApiKey,
) -> ProviderError {
    let error = serde_json::from_str::<JsonValue>(body)
        .ok()
        .and_then(|value| value.get("error").cloned());
    let summary = error
        .as_ref()
        .and_then(summarize)
        .unwrap_or_else(|| body.trim().to_string());
    let header = retry_after
        .and_then(|value| value.trim().parse::<u64>().ok())
        .map(Duration::from_secs);
    let mapped = ProviderError::Status {
        status,
        retry_after: header.or_else(|| error.as_ref().and_then(retry_delay)),
        message: clean(&summary, key),
    };
    overflow::GEMINI.classify(mapped, &[])
}

/// An `error` object received inside a 200 stream, mapped by its `code`
/// like the same error outside a stream.
pub(crate) fn stream_error(error: &JsonValue, key: &ApiKey) -> ProviderError {
    let message = clean(&summarize(error).unwrap_or_else(|| error.to_string()), key);
    match error
        .get("code")
        .and_then(JsonValue::as_u64)
        .and_then(|code| u16::try_from(code).ok())
    {
        Some(status) => overflow::GEMINI.classify(
            ProviderError::Status {
                status,
                retry_after: retry_delay(error),
                message,
            },
            &[],
        ),
        None => ProviderError::Transport { message },
    }
}

fn summarize(error: &JsonValue) -> Option<String> {
    let message = error.get("message").and_then(JsonValue::as_str)?;
    Some(match error.get("status").and_then(JsonValue::as_str) {
        Some(status) => format!("{status}: {message}"),
        None => message.to_string(),
    })
}

/// `RetryInfo.retryDelay`, a decimal number of seconds ending in `s`.
fn retry_delay(error: &JsonValue) -> Option<Duration> {
    error
        .get("details")?
        .as_array()?
        .iter()
        .filter(|detail| detail.get("@type").and_then(JsonValue::as_str) == Some(RETRY_INFO))
        .find_map(|detail| {
            let delay = detail.get("retryDelay")?.as_str()?.strip_suffix('s')?;
            let seconds: f64 = delay.parse().ok()?;
            Duration::try_from_secs_f64(seconds).ok()
        })
}

/// The summary without the key. The cut to 500 characters comes last, after
/// every secret is out (`RequestOptions::scrub_error`, `sse::events`).
fn clean(summary: &str, key: &ApiKey) -> String {
    key.redact(summary)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    const SECRET: &str = "AIzaSy-unit-test-secret-0042";

    fn key() -> ApiKey {
        ApiKey::new(SECRET).unwrap()
    }

    /// Gemini's 400 for an input over the model's token limit is a
    /// context-length error, in a response or inside a stream; another
    /// invalid argument is not.
    #[test]
    fn an_input_over_the_token_limit_is_a_context_length_error() {
        let refusal = json!({"code": 400, "status": "INVALID_ARGUMENT", "message":
            "The input token count (35868) exceeds the maximum number of tokens allowed (32768)."});
        let body = json!({"error": refusal}).to_string();
        let err = status_error(400, None, &body, &key());
        assert!(
            matches!(&err, ProviderError::ContextLength { status: Some(400), message }
                if message.starts_with("INVALID_ARGUMENT: The input token count")),
            "{err:?}"
        );
        assert!(matches!(
            stream_error(&refusal, &key()),
            ProviderError::ContextLength { .. }
        ));
        let other = r#"{"error":{"code":400,"message":"Invalid JSON payload","status":"INVALID_ARGUMENT"}}"#;
        assert!(matches!(
            status_error(400, None, other, &key()),
            ProviderError::Status { .. }
        ));
    }

    #[test]
    fn google_error_body_is_summarized_as_status_and_message() {
        let body = r#"{"error":{"code":400,"message":"Invalid JSON payload","status":"INVALID_ARGUMENT"}}"#;
        let err = status_error(400, None, body, &key());
        assert!(
            matches!(&err, ProviderError::Status { status: 400, retry_after: None, message }
                if message == "INVALID_ARGUMENT: Invalid JSON payload"),
            "{err:?}"
        );
        assert!(!err.retryable());
    }

    #[test]
    fn retry_delay_comes_from_the_header_then_retry_info() {
        let body =
            json!({"error": {"code": 429, "message": "Quota", "status": "RESOURCE_EXHAUSTED",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure"},
                {"@type": RETRY_INFO, "retryDelay": "1.5s"},
            ]}})
            .to_string();
        let delay = |header| match status_error(429, header, &body, &key()) {
            ProviderError::Status { retry_after, .. } => retry_after,
            other => panic!("{other:?}"),
        };
        assert_eq!(delay(None), Some(Duration::from_millis(1500)));
        assert_eq!(delay(Some("3")), Some(Duration::from_secs(3)));
        let bad = json!({"error": {"details": [{"@type": RETRY_INFO, "retryDelay": "-1s"}]}});
        assert!(matches!(
            status_error(429, None, &bad.to_string(), &key()),
            ProviderError::Status {
                retry_after: None,
                ..
            }
        ));
    }

    #[test]
    fn a_body_echoing_the_key_is_redacted() {
        let body = format!(
            r#"{{"error":{{"code":400,"message":"API key not valid: x-goog-api-key={SECRET}","status":"INVALID_ARGUMENT"}}}}"#
        );
        for err in [
            status_error(400, None, &body, &key()),
            status_error(500, None, &format!("echo {SECRET}"), &key()),
        ] {
            assert!(!format!("{err} {err:?}").contains(SECRET), "{err:?}");
        }
    }

    #[test]
    fn stream_errors_use_their_code() {
        let err = stream_error(
            &json!({"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}),
            &key(),
        );
        assert!(
            matches!(&err, ProviderError::Status { status: 503, message, .. }
                if message == "UNAVAILABLE: overloaded"),
            "{err:?}"
        );
        assert!(err.retryable());
        let no_code = stream_error(&json!({"message": format!("bad {SECRET}")}), &key());
        assert!(
            matches!(&no_code, ProviderError::Transport { message } if !message.contains(SECRET))
        );
    }
}

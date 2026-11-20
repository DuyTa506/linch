//! The streaming POST and HTTP failure mapping.

use std::time::Duration;

use linch_core::{JsonValue, ProviderError};
use reqwest::header::{ACCEPT, RETRY_AFTER};
use tokio_util::sync::CancellationToken;

use crate::ApiKey;
use crate::api_key::scrub;
use crate::overflow::Overflow;

/// POST `body` to `url` and return the response once its status is 2xx.
///
/// `key`, when given, is sent as a bearer token. A redirect is refused,
/// never followed; any other non-2xx status reads the start of the body and
/// maps it with [`status_error`], under the target's `overflow`.
pub(crate) async fn post(
    client: &reqwest::Client,
    url: &str,
    key: Option<&ApiKey>,
    body: &JsonValue,
    overflow: &Overflow,
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
        overflow,
    ))
}

/// A request that got no response, with its text redacted: a
/// [`ProviderError::Connect`] when the connection was never made, so the
/// request never went out.
pub(crate) fn transport(key: Option<&ApiKey>, err: &reqwest::Error) -> ProviderError {
    let message = scrub(key, &err.to_string());
    if err.is_connect() {
        ProviderError::Connect { message }
    } else {
        ProviderError::Transport { message }
    }
}

/// Map a non-2xx response to [`ProviderError::Status`], or to
/// [`ProviderError::ContextLength`] when the target's `overflow` says the
/// prompt was too long. That is judged on the error before it is cut: its
/// `code` and `type`, and its whole message, nested under `error` or flat
/// (older vLLM, SGLang).
///
/// The summary prefers the OpenAI-style `error.message`. It is redacted of
/// the key here; the cut to 500 characters comes last, after every secret
/// is out (`RequestOptions::scrub_error`).
/// `Retry-After` is read as whole seconds; an HTTP date is ignored.
pub(crate) fn status_error(
    status: u16,
    retry_after: Option<&str>,
    body: &str,
    key: Option<&ApiKey>,
    overflow: &Overflow,
) -> ProviderError {
    let value = serde_json::from_str::<JsonValue>(body).ok();
    let error = (value.as_ref()).map(|value| value.get("error").unwrap_or(value));
    let text = |field: &str| error.and_then(|error| error.get(field)?.as_str());
    let labels: Vec<&str> = [text("code"), text("type")].into_iter().flatten().collect();
    let refused = overflow.matches(Some(status), &labels, text("message").unwrap_or(body));
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
    let message = scrub(key, &summary);
    if refused {
        return ProviderError::ContextLength {
            status: Some(status),
            message,
        };
    }
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
    use crate::overflow::OPENAI_CHAT;

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

    /// Each OpenAI-compatible target maps its own refusal of a prompt over
    /// the context window, with the error nested or flat, and no other's.
    #[test]
    fn each_target_maps_its_own_context_length_refusal() {
        use crate::overflow::{DEEPSEEK, LLAMACPP, SGLANG, VLLM};
        let openai = r#"{"error":{"message":"This model's maximum context length is 128000 tokens. However, your messages resulted in 130000 tokens.","type":"invalid_request_error","param":"messages","code":"context_length_exceeded"}}"#;
        let deepseek = r#"{"error":{"message":"This model's maximum context length is 131072 tokens. However, you requested 131329 tokens (123137 in the messages, 8192 in the completion).","type":"invalid_request_error","param":null,"code":"invalid_request_error"}}"#;
        let vllm_flat = r#"{"object":"error","message":"This model's maximum context length is 4096 tokens. However, you requested 5000 tokens (4000 in the messages, 1000 in the completion).","type":"BadRequestError","param":null,"code":400}"#;
        let sglang = r#"{"object":"error","message":"Input length (9000 tokens) exceeds the maximum allowed length (8192 tokens).","type":"BadRequest","param":null,"code":400}"#;
        let llama = r#"{"error":{"code":400,"message":"request (9000 tokens) exceeds the available context size (8192 tokens), try increasing it","type":"exceed_context_size_error","n_prompt_tokens":9000,"n_ctx":8192}}"#;
        let refused = |body: &str, overflow: &Overflow| {
            matches!(
                status_error(400, None, body, Some(&key()), overflow),
                ProviderError::ContextLength {
                    status: Some(400),
                    ..
                }
            )
        };
        for (body, overflow) in [
            (openai, &OPENAI_CHAT),
            (deepseek, &DEEPSEEK),
            (vllm_flat, &VLLM),
            (sglang, &SGLANG),
            (llama, &LLAMACPP),
        ] {
            assert!(refused(body, overflow), "{body}");
        }
        assert!(!refused(sglang, &OPENAI_CHAT), "not OpenAI's wording");
        assert!(!refused(llama, &VLLM), "not vLLM's wording");
        let other = r#"{"error":{"message":"bad tool","type":"invalid_request_error"}}"#;
        assert!(!refused(other, &OPENAI_CHAT));
        let throttled = status_error(429, None, openai, Some(&key()), &OPENAI_CHAT);
        assert!(matches!(
            throttled,
            ProviderError::Status { status: 429, .. }
        ));
    }

    #[test]
    fn error_message_and_type_are_summarized() {
        let body = r#"{"error":{"message":"Insufficient Balance","type":"unknown_error","param":null,"code":"invalid_request_error"}}"#;
        let err = status_error(402, None, body, Some(&key()), &OPENAI_CHAT);
        assert!(
            matches!(&err, ProviderError::Status { status: 402, retry_after: None, message }
                if message == "unknown_error: Insufficient Balance"),
            "{err:?}"
        );
        assert!(!err.retryable());
    }

    #[test]
    fn rate_limit_keeps_retry_after_seconds() {
        let err = status_error(429, Some(" 7 "), "{}", Some(&key()), &OPENAI_CHAT);
        assert!(
            matches!(&err, ProviderError::Status { status: 429, retry_after: Some(delay), .. }
                if *delay == Duration::from_secs(7)),
            "{err:?}"
        );
        assert!(err.retryable());
        let dated = status_error(
            429,
            Some("Wed, 21 Oct 2015 07:28:00 GMT"),
            "",
            Some(&key()),
            &OPENAI_CHAT,
        );
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
        let err = status_error(401, None, &body, Some(&key()), &OPENAI_CHAT);
        assert_clean(&err);
        assert!(err.to_string().contains("Bearer [redacted]"), "{err}");
    }

    #[test]
    fn rate_limit_plain_body_echoing_headers_is_redacted() {
        let body = format!(
            "429 Too Many Requests\nrequest headers: authorization=Bearer {SECRET}; x-api-key={SECRET}"
        );
        let err = status_error(429, Some("2"), &body, Some(&key()), &OPENAI_CHAT);
        assert_clean(&err);
    }

    #[test]
    fn a_long_body_is_redacted_whole() {
        let body = format!("{}{SECRET}", "x".repeat(500 - 5));
        let err = status_error(500, None, &body, Some(&key()), &OPENAI_CHAT);
        assert_clean(&err);
    }
}

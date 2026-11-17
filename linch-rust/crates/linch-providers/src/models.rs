//! Asking a vendor's API which models it lists.
//!
//! One GET helper for every adapter ([`get_json`]) and one parser per list
//! shape. The helper never puts the key or the URL in an error: a failure
//! to connect, a timeout and a bad answer each have a fixed message, and a
//! vendor's error text is scrubbed of the key first.
//!
//! List shapes, checked on 2026-11-17 against the vendors' references:
//! - OpenAI (`GET /models`): `data[].id`, no pagination
//!   (<https://developers.openai.com/api/reference/resources/models/methods/list>).
//! - vLLM and SGLang: the same, with `max_model_len` per model.
//! - DeepSeek (<https://api-docs.deepseek.com/api/list-models>): `id`, and
//!   when known `context_window`, `max_output_tokens` and
//!   `input_modalities`.
//! - Anthropic (<https://platform.claude.com/docs/en/api/models/list>):
//!   `data[]` with `id`, `display_name`, `max_input_tokens`, `max_tokens`
//!   and `capabilities.image_input`; `limit` up to 1000, paged by
//!   `after_id` while `has_more`.
//! - Gemini (`GET /v1beta/models`): `models[]` with `name`
//!   (`models/{id}`), `displayName`, `inputTokenLimit`, `outputTokenLimit`
//!   and `supportedGenerationMethods`, paged by `pageSize` and
//!   `pageToken` while `nextPageToken`.

use std::time::Duration;

use linch_core::{JsonValue, ModelInfo, ProviderError};
use reqwest::header::{ACCEPT, RETRY_AFTER};

use crate::ApiKey;
use crate::api_key::scrub;

/// How long one list request may take in all.
const LIST_TIMEOUT: Duration = Duration::from_secs(15);

/// Most pages followed, so a server that always says "more" ends.
pub(crate) const MAX_PAGES: usize = 10;

/// Most bytes of a list read.
const MAX_BODY: usize = 4 * 1024 * 1024;

/// Most bytes of an error answer read, as the other adapters read.
const MAX_ERROR_BODY: usize = 8 * 1024;

/// Longest vendor error text kept. It is untrusted: a host may show it to
/// a tenant, so it stays short.
const MAX_ERROR_CHARS: usize = 120;

/// Longest `display_name` kept.
const MAX_NAME_CHARS: usize = 100;

/// GET `url` with `headers` and decode the JSON body of a 2xx answer.
///
/// `key` is only used to scrub the text of the vendor's error.
pub(crate) async fn get_json(
    client: &reqwest::Client,
    url: &str,
    headers: &[(&str, &str)],
    key: Option<&ApiKey>,
) -> Result<JsonValue, ProviderError> {
    let mut request = client
        .get(url)
        .timeout(LIST_TIMEOUT)
        .header(ACCEPT, "application/json");
    for (name, value) in headers {
        request = request.header(*name, *value);
    }
    let mut response = request.send().await.map_err(|err| {
        let message = if err.is_timeout() {
            "the provider did not answer the model list in time"
        } else if err.is_connect() {
            "could not connect to the provider to list models"
        } else {
            "the request for the model list failed"
        };
        if err.is_connect() {
            ProviderError::Connect {
                message: message.into(),
            }
        } else {
            ProviderError::Transport {
                message: message.into(),
            }
        }
    })?;
    let status = response.status();
    if status.is_redirection() {
        return Err(crate::transport::redirect_refused(status.as_u16()));
    }
    let retry_after = response
        .headers()
        .get(RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.trim().parse::<u64>().ok())
        .map(Duration::from_secs);
    // An error answer is read to its first bytes only.
    let limit = if status.is_success() {
        MAX_BODY
    } else {
        MAX_ERROR_BODY
    };
    let mut body = Vec::new();
    while body.len() < limit {
        match response.chunk().await {
            Ok(Some(chunk)) => body.extend_from_slice(&chunk),
            Ok(None) => break,
            Err(_) => {
                return Err(ProviderError::Transport {
                    message: "the model list broke off".into(),
                });
            }
        }
    }
    if !status.is_success() {
        return Err(ProviderError::Status {
            status: status.as_u16(),
            retry_after,
            message: error_summary(&body[..body.len().min(limit)], key),
        });
    }
    if body.len() >= MAX_BODY {
        return Err(ProviderError::Transport {
            message: "the model list is too large".into(),
        });
    }
    serde_json::from_slice(&body).map_err(|_| ProviderError::Transport {
        message: "the provider's model list is not JSON".into(),
    })
}

/// `error.message` of a vendor's error body (OpenAI, Anthropic and Google
/// all nest it so), scrubbed, reduced to ASCII printable characters and
/// cut to [`MAX_ERROR_CHARS`]; a body of any other shape gives no text,
/// since it could echo the request.
fn error_summary(body: &[u8], key: Option<&ApiKey>) -> String {
    let value = serde_json::from_slice::<JsonValue>(body).ok();
    let message = value
        .as_ref()
        .and_then(|value| value.get("error")?.get("message")?.as_str());
    match message {
        Some(message) => (scrub(key, message).chars())
            .filter(|c| c.is_ascii_graphic() || *c == ' ')
            .take(MAX_ERROR_CHARS)
            .collect(),
        None => "the provider refused the model list".into(),
    }
}

/// The rows of `value[field]`.
fn rows<'a>(value: &'a JsonValue, field: &str) -> &'a [JsonValue] {
    value
        .get(field)
        .and_then(JsonValue::as_array)
        .map_or(&[], Vec::as_slice)
}

fn text(row: &JsonValue, field: &str) -> Option<String> {
    let text = row.get(field)?.as_str()?;
    (!text.is_empty()).then(|| text.to_owned())
}

/// A name for a person to read: no control or direction-changing
/// characters, at most [`MAX_NAME_CHARS`], and `None` when nothing is left.
fn display_name(row: &JsonValue, field: &str) -> Option<String> {
    let hidden = |c: &char| {
        c.is_control()
            || matches!(*c, '\u{200b}'..='\u{200f}' | '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}' | '\u{feff}')
    };
    let name: String = (row.get(field)?.as_str()?.chars())
        .filter(|c| !hidden(c))
        .take(MAX_NAME_CHARS)
        .collect();
    let name = name.trim();
    (!name.is_empty()).then(|| name.to_owned())
}

fn count(row: &JsonValue, field: &str) -> Option<u64> {
    row.get(field)?.as_u64().filter(|&n| n > 0)
}

/// Which extra members of an OpenAI-style list row a target gives.
#[derive(Debug, Clone, Copy)]
pub(crate) struct OpenAiRow {
    /// The member holding the context window, if the target has one.
    pub context: Option<&'static str>,
    /// The member holding the output limit, if the target has one.
    pub output: Option<&'static str>,
    /// Whether `input_modalities` is read.
    pub modalities: bool,
}

/// An OpenAI-style list: `data[]` of rows with an `id`.
pub(crate) fn openai_list(value: &JsonValue, row: OpenAiRow) -> Vec<ModelInfo> {
    rows(value, "data")
        .iter()
        .filter_map(|item| {
            let mut info = ModelInfo::new(text(item, "id")?);
            info.display_name = display_name(item, "name");
            info.context_window = row.context.and_then(|field| count(item, field));
            info.max_output_tokens = row.output.and_then(|field| count(item, field));
            if row.modalities {
                info.input_modalities = (rows(item, "input_modalities").iter())
                    .filter_map(|m| m.as_str().map(str::to_owned))
                    .collect();
            }
            Some(info)
        })
        .collect()
}

/// One page of Anthropic's list, and the id to continue after.
pub(crate) fn anthropic_page(value: &JsonValue) -> (Vec<ModelInfo>, Option<String>) {
    let models = rows(value, "data")
        .iter()
        .filter_map(|item| {
            let mut info = ModelInfo::new(text(item, "id")?);
            info.display_name = display_name(item, "display_name");
            info.context_window = count(item, "max_input_tokens");
            info.max_output_tokens = count(item, "max_tokens");
            let image = item
                .get("capabilities")
                .and_then(|caps| caps.get("image_input")?.get("supported")?.as_bool());
            if let Some(image) = image {
                info.input_modalities = ["text"]
                    .into_iter()
                    .chain(image.then_some("image"))
                    .map(str::to_owned)
                    .collect();
            }
            Some(info)
        })
        .collect();
    let more = value.get("has_more").and_then(JsonValue::as_bool) == Some(true);
    let after = more.then(|| text(value, "last_id")).flatten();
    (models, after)
}

/// One page of Gemini's list, and the token to continue with. A model
/// whose methods lack `generateContent` (an embedding model) is left out.
pub(crate) fn gemini_page(value: &JsonValue) -> (Vec<ModelInfo>, Option<String>) {
    let models = rows(value, "models")
        .iter()
        .filter(|item| {
            let methods = rows(item, "supportedGenerationMethods");
            methods.is_empty()
                || methods
                    .iter()
                    .any(|m| m.as_str() == Some("generateContent"))
        })
        .filter_map(|item| {
            let name = text(item, "name")?;
            let mut info = ModelInfo::new(name.strip_prefix("models/").unwrap_or(&name));
            info.display_name = display_name(item, "displayName");
            info.context_window = count(item, "inputTokenLimit");
            info.max_output_tokens = count(item, "outputTokenLimit");
            Some(info)
        })
        .collect();
    (models, text(value, "nextPageToken"))
}

#[cfg(test)]
mod tests;

//! Options every adapter takes for its HTTP requests.
//!
//! [`RequestOptions`] is what Python's `extra_body`, `default_headers` and
//! `timeout` provider options become: raw fields merged into the request
//! body, headers sent with every request, and the longest silence the
//! connection may keep.

use std::time::Duration;

use linch_core::{JsonObject, JsonValue, ProviderError};
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};

/// Body fields an adapter owns: `extra_body` never replaces them. The
/// request's own `temperature` always wins, as in Python.
/// The conversation and tools stay what the session logged, so they are
/// protected too.
const PROTECTED_FIELDS: [&str; 9] = [
    "model",
    "stream",
    "temperature",
    "messages",
    "tools",
    "input",
    "contents",
    "system",
    "instructions",
];

const CHAT_TEMPLATE_KWARGS: &str = "chat_template_kwargs";

/// Headers that carry the provider's key. A caller that sets one sends its
/// own credential, and the adapter then leaves the environment's key out.
const KEY_HEADERS: [&str; 3] = ["authorization", "x-api-key", "x-goog-api-key"];
/// Headers a caller cannot set: the ones the HTTP client owns.
const FORBIDDEN_HEADERS: [&str; 7] = [
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "upgrade",
    "te",
    "trailer",
];

/// Extra request settings, the same for every adapter.
///
/// The default adds nothing: no body fields, no headers, the adapter's own
/// timeout.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct RequestOptions {
    /// Top-level fields merged into every request body, for vendor
    /// parameters the adapter does not know (`top_k`, `cache_salt`, ...).
    /// They replace a field the adapter sets, except `model`, `stream` and
    /// `temperature`, which the request keeps.
    pub extra_body: JsonObject,
    /// Longest silence, between two chunks of the response, before the
    /// call fails; `None` keeps the adapter's own limit.
    pub timeout: Option<Duration>,
    headers: HeaderMap,
}

impl RequestOptions {
    /// Add a header sent with every request. A value that may be a
    /// credential is hidden from `Debug` output.
    ///
    /// A key header (`authorization`, `x-api-key`, `x-goog-api-key`) sends
    /// the caller's credential in place of the provider key from the
    /// environment, which is then not sent; see
    /// [`RequestOptions::sets_key_header`].
    ///
    /// # Errors
    ///
    /// [`ProviderError::InvalidRequest`] when `name` or `value` is not a
    /// valid header, or `name` is one the HTTP client sets itself (`host`,
    /// `content-length`, `transfer-encoding`, `connection`, `upgrade`, `te`,
    /// `trailer`). The value is not part of the message.
    pub fn header(mut self, name: &str, value: &str) -> Result<Self, ProviderError> {
        let invalid = |what: &str| ProviderError::InvalidRequest {
            message: format!("header {name:?} is not allowed: {what}"),
        };
        let header_name = HeaderName::from_bytes(name.as_bytes())
            .map_err(|_| invalid("not a valid header name"))?;
        if FORBIDDEN_HEADERS.contains(&header_name.as_str()) {
            return Err(invalid("the HTTP client sets it"));
        }
        let mut header_value =
            HeaderValue::from_str(value).map_err(|_| invalid("its value is not valid"))?;
        header_value.set_sensitive(true);
        self.headers.insert(header_name, header_value);
        Ok(self)
    }

    /// Whether a header carries the caller's own credential. The adapter
    /// then does not send the provider key.
    pub fn sets_key_header(&self) -> bool {
        KEY_HEADERS
            .iter()
            .any(|name| self.headers.contains_key(*name))
    }

    /// Merge `extra_body` into `body`, which must be a JSON object.
    pub(crate) fn merge_body(&self, body: &mut JsonValue) {
        if let Some(object) = body.as_object_mut() {
            merge(object, &self.extra_body);
        }
    }

    /// The HTTP client for an adapter: its default headers, and its read
    /// timeout unless [`RequestOptions::timeout`] sets one.
    pub(crate) fn client(
        &self,
        connect: Duration,
        read: Duration,
    ) -> reqwest::Result<reqwest::Client> {
        crate::transport::client(connect, self.timeout.unwrap_or(read), &self.headers)
    }
}

/// Merge `extra` into `body`: each field of `extra` replaces the field of
/// the same name, except [`PROTECTED_FIELDS`].
///
/// `chat_template_kwargs` is the exception to replacing: when both sides
/// hold an object, their fields are merged and `extra` wins per field.
pub(crate) fn merge(body: &mut JsonObject, extra: &JsonObject) {
    for (name, value) in extra {
        if PROTECTED_FIELDS.contains(&name.as_str()) {
            continue;
        }
        if name == CHAT_TEMPLATE_KWARGS
            && let (Some(JsonValue::Object(have)), JsonValue::Object(add)) =
                (body.get_mut(name), value)
        {
            have.extend(add.clone());
            continue;
        }
        body.insert(name.clone(), value.clone());
    }
}

/// Settings for the Chat Completions adapters (OpenAI, DeepSeek, vLLM,
/// SGLang, llama.cpp).
#[derive(Debug, Clone, PartialEq)]
pub struct ChatOptions {
    /// Sent as `parallel_tool_calls` when the request has tools; `None`
    /// leaves the server's default.
    pub parallel_tool_calls: Option<bool>,
    /// Whether to ask for usage in the last stream chunk
    /// (`stream_options.include_usage`). On by default; without it a
    /// server that needs the option reports no usage.
    pub include_stream_options: bool,
    /// Fields of `chat_template_kwargs` for servers that apply a chat
    /// template (vLLM, SGLang, llama.cpp). With thinking off, the
    /// server's thinking switch is merged over these.
    pub chat_template_kwargs: JsonObject,
    /// Native sampling fields (`top_k`, `min_p`, ...) merged into the
    /// body, under `extra_body`.
    pub sampling_params: JsonObject,
}

impl Default for ChatOptions {
    fn default() -> Self {
        Self {
            parallel_tool_calls: None,
            include_stream_options: true,
            chat_template_kwargs: JsonObject::new(),
            sampling_params: JsonObject::new(),
        }
    }
}

impl ChatOptions {
    /// Apply these settings to a built Chat Completions body.
    pub(crate) fn apply(&self, body: &mut JsonValue) {
        let has_tools = body.get("tools").is_some();
        let Some(object) = body.as_object_mut() else {
            return;
        };
        if !self.include_stream_options {
            object.remove("stream_options");
        }
        if let (Some(parallel), true) = (self.parallel_tool_calls, has_tools) {
            object.insert("parallel_tool_calls".into(), parallel.into());
        }
        merge(object, &self.sampling_params);
        if !self.chat_template_kwargs.is_empty() {
            let mut kwargs = self.chat_template_kwargs.clone();
            if let Some(JsonValue::Object(thinking)) = object.remove(CHAT_TEMPLATE_KWARGS) {
                kwargs.extend(thinking);
            }
            object.insert(CHAT_TEMPLATE_KWARGS.into(), kwargs.into());
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn object(value: JsonValue) -> JsonObject {
        value.as_object().cloned().unwrap()
    }

    #[test]
    fn extra_body_cannot_replace_the_conversation_or_tools() {
        let options = RequestOptions {
            extra_body: object(json!({"messages": [], "tools": [], "input": 1, "system": "x"})),
            ..RequestOptions::default()
        };
        let mut body = json!({"messages": [1], "tools": [2]});
        options.merge_body(&mut body);
        assert_eq!(body, json!({"messages": [1], "tools": [2]}));
    }

    #[test]
    fn extra_body_replaces_fields_but_not_the_protected_ones() {
        let options = RequestOptions {
            extra_body: object(json!({
                "top_k": 40, "max_tokens": 9, "model": "x", "stream": false, "temperature": 2.0,
            })),
            ..RequestOptions::default()
        };
        let mut body = json!({"model": "m", "stream": true, "temperature": 0.5, "max_tokens": 1});
        options.merge_body(&mut body);
        assert_eq!(
            body,
            json!({"model": "m", "stream": true, "temperature": 0.5, "max_tokens": 9, "top_k": 40})
        );
    }

    #[test]
    fn chat_options_shape_a_chat_body() {
        let chat = ChatOptions {
            parallel_tool_calls: Some(false),
            include_stream_options: false,
            chat_template_kwargs: object(json!({"a": 1, "enable_thinking": true})),
            sampling_params: object(json!({"top_k": 40})),
        };
        let mut body = json!({
            "model": "m", "stream": true, "stream_options": {"include_usage": true},
            "tools": [{}], "chat_template_kwargs": {"enable_thinking": false},
        });
        chat.apply(&mut body);
        assert_eq!(
            body,
            json!({
                "model": "m", "stream": true, "tools": [{}], "parallel_tool_calls": false,
                "top_k": 40, "chat_template_kwargs": {"a": 1, "enable_thinking": false},
            })
        );
        let mut no_tools = json!({"model": "m"});
        chat.apply(&mut no_tools);
        assert!(no_tools.get("parallel_tool_calls").is_none());
    }

    #[test]
    fn extra_body_merges_chat_template_kwargs_per_field() {
        let options = RequestOptions {
            extra_body: object(json!({"chat_template_kwargs": {"b": 2, "a": 9}})),
            ..RequestOptions::default()
        };
        let mut body = json!({"chat_template_kwargs": {"a": 1, "c": 3}});
        options.merge_body(&mut body);
        assert_eq!(
            body,
            json!({"chat_template_kwargs": {"a": 9, "b": 2, "c": 3}})
        );
    }

    #[test]
    fn headers_the_client_owns_are_refused_without_echoing_the_value() {
        for name in ["Host", "content-length", "Transfer-Encoding", "connection"] {
            let err = RequestOptions::default()
                .header(name, "secret")
                .unwrap_err();
            assert!(
                matches!(err, ProviderError::InvalidRequest { .. }),
                "{name}"
            );
            assert!(!err.to_string().contains("secret"), "{name}");
        }
        let err = RequestOptions::default()
            .header("bad name", "v")
            .unwrap_err();
        assert!(matches!(err, ProviderError::InvalidRequest { .. }));
        let err = RequestOptions::default()
            .header("x-a", "line\nbreak")
            .unwrap_err();
        assert!(!err.to_string().contains("line"));
    }

    #[test]
    fn a_header_value_stays_out_of_debug_output() {
        let options = RequestOptions::default()
            .header("x-gateway-token", "very-secret")
            .unwrap();
        assert!(!format!("{options:?}").contains("very-secret"));
    }
}

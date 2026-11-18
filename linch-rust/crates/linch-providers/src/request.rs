//! Options every adapter takes for its HTTP requests.
//!
//! [`RequestOptions`] is what Python's `extra_body`, `default_headers` and
//! `timeout` provider options become: raw fields merged into the request
//! body, headers sent with every request, and the longest silence the
//! connection may keep.

use std::time::Duration;

use futures::StreamExt;
use linch_core::{JsonObject, JsonValue, ProviderError, ProviderStream};
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};

use crate::ApiKey;
use crate::HttpClients;
use crate::clients::SharedClient;

/// Body fields an adapter owns: `extra_body` never replaces them. The
/// request's own `temperature` always wins, as in Python.
///
/// The conversation, the tools and how the model may choose among them stay
/// what the session logged and admitted (each wire's spelling is listed),
/// and so do the fields that would hand state to the vendor outside the
/// session log (`store`, `previous_response_id`, `conversation`). The
/// Gemini `generationConfig` is merged field by field instead
/// ([`merge`]), so its `temperature` stays the request's.
const PROTECTED_FIELDS: [&str; 17] = [
    "model",
    "stream",
    "temperature",
    "messages",
    "tools",
    "tool_choice",
    "input",
    "contents",
    "system",
    "instructions",
    "systemInstruction",
    "system_instruction",
    "toolConfig",
    "tool_config",
    "store",
    "previous_response_id",
    "conversation",
];

/// Gemini's settings object, which holds the request's temperature.
const GENERATION_CONFIG: &str = "generationConfig";

/// Values of other headers shorter than this are not scrubbed from error
/// text: a word that short would mangle the message and is no secret. A
/// credential-named header (see `holds_credential`) is scrubbed at any
/// length.
const MIN_SCRUBBED_VALUE: usize = 8;

/// Parts of a header name that mark its value as a credential.
const CREDENTIAL_NAMES: [&str; 5] = ["token", "key", "secret", "auth", "cookie"];

const REDACTED: &str = "[redacted]";

const CHAT_TEMPLATE_KWARGS: &str = "chat_template_kwargs";

/// Headers that carry the provider's key. A caller that sets one sends its
/// own credential, and the adapter then leaves the environment's key out.
const KEY_HEADERS: [&str; 4] = ["authorization", "x-api-key", "x-goog-api-key", "api-key"];
/// Headers a caller cannot set: the ones the HTTP client owns.
const FORBIDDEN_HEADERS: [&str; 9] = [
    "proxy-authorization",
    "proxy-connection",
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "upgrade",
    "te",
    "trailer",
];

/// Whether `name` is a header that carries a provider key
/// (`authorization`, `x-api-key`, `x-goog-api-key`, `api-key`), whatever
/// its case.
pub fn is_key_header(name: &str) -> bool {
    KEY_HEADERS.iter().any(|key| key.eq_ignore_ascii_case(name))
}

/// Whether the value of header `name` (lower case) is a credential: a key
/// header, or a name that says token, key, secret, auth or cookie.
fn holds_credential(name: &str) -> bool {
    is_key_header(name) || CREDENTIAL_NAMES.iter().any(|part| name.contains(part))
}

/// A per-tenant value that keeps a provider's prompt-prefix cache from being
/// shared with another tenant on the same key and endpoint.
///
/// It is secret: `Debug` hides it, an error text a server echoes back is
/// scrubbed of it, and it is set at open, never read from a config. See
/// [`RequestOptions::cache_salt`].
#[derive(Clone, PartialEq, Eq)]
pub struct CacheSalt(String);

impl CacheSalt {
    /// A salt of `value`; `None` when it is empty, which no wire takes.
    pub fn new(value: impl Into<String>) -> Option<Self> {
        let value = value.into();
        (!value.is_empty()).then_some(Self(value))
    }

    /// The salt as sent on the wire.
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for CacheSalt {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("CacheSalt([redacted])")
    }
}

/// Extra request settings, the same for every adapter.
///
/// The default adds nothing: no body fields, no headers, the adapter's own
/// timeout.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct RequestOptions {
    /// Top-level fields merged into every request body, for vendor
    /// parameters the adapter does not know (`top_k`, `cache_salt`, ...).
    /// They replace a field the adapter sets, except the ones it owns
    /// (see `PROTECTED_FIELDS`: the model, `stream`, `temperature`, the
    /// conversation, the tools and `tool_choice`), which the request keeps.
    pub extra_body: JsonObject,
    /// Longest silence, between two chunks of the response, before the
    /// call fails; `None` keeps the adapter's own limit.
    pub timeout: Option<Duration>,
    /// Keeps this agent's prompt-prefix cache apart from other tenants' on
    /// the same provider key. Sent where the vendor has a field for it:
    /// `cache_salt` to a vLLM server, `prompt_cache_key` to OpenAI (Chat
    /// Completions and Responses); never sent to the other adapters, which
    /// have none. It goes in after `extra_body`, which cannot replace it.
    /// `None` sends nothing.
    pub cache_salt: Option<CacheSalt>,
    headers: HeaderMap,
    /// Where the HTTP client comes from, when a holder shares them.
    clients: Option<HttpClients>,
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

    /// The provider key an adapter sends, `key` unless the caller's own key
    /// header replaces it. Every call (a turn, a model list, a window probe)
    /// asks here, so none of them sends both credentials.
    pub(crate) fn key_to_send<'a>(&self, key: &'a ApiKey) -> Option<&'a ApiKey> {
        (!self.sets_key_header()).then_some(key)
    }

    /// `text` without any header value of 8 bytes or more, and without the
    /// value of a credential-named header whatever its length. A server can
    /// echo a request header in an error; the header's value is a secret
    /// even when the provider key is not.
    pub(crate) fn scrub(&self, text: &str) -> String {
        let mut text = text.to_owned();
        if let Some(salt) = &self.cache_salt {
            text = text.replace(salt.expose(), REDACTED);
        }
        for (name, value) in &self.headers {
            if let Ok(value) = value.to_str()
                && !value.is_empty()
                && (value.len() >= MIN_SCRUBBED_VALUE || holds_credential(name.as_str()))
            {
                text = text.replace(value, REDACTED);
            }
        }
        text
    }

    /// `err` with its message scrubbed of the header values.
    pub(crate) fn scrub_error(&self, err: ProviderError) -> ProviderError {
        if self.headers.is_empty() && self.cache_salt.is_none() {
            return err;
        }
        match err {
            ProviderError::Transport { message } => ProviderError::Transport {
                message: self.scrub(&message),
            },
            ProviderError::Status {
                status,
                retry_after,
                message,
            } => ProviderError::Status {
                status,
                retry_after,
                message: self.scrub(&message),
            },
            ProviderError::ContextLength { status, message } => ProviderError::ContextLength {
                status,
                message: self.scrub(&message),
            },
            other => other,
        }
    }

    /// `stream` with the errors it yields scrubbed of the header values.
    pub(crate) fn scrub_stream(&self, stream: ProviderStream) -> ProviderStream {
        if self.headers.is_empty() && self.cache_salt.is_none() {
            return stream;
        }
        let options = self.clone();
        stream
            .map(move |item| item.map_err(|err| options.scrub_error(err)))
            .boxed()
    }

    /// Merge `extra_body` into `body`, which must be a JSON object.
    pub(crate) fn merge_body(&self, body: &mut JsonValue) {
        if let Some(object) = body.as_object_mut() {
            merge(object, &self.extra_body);
        }
    }

    /// Put the cache salt in `body` as `field`, the vendor's name for it,
    /// after `extra_body` and the sampling fields, so neither replaces it.
    /// Nothing when no salt is set.
    pub(crate) fn apply_cache_salt(&self, body: &mut JsonValue, field: &str) {
        if let (Some(salt), Some(object)) = (&self.cache_salt, body.as_object_mut()) {
            object.insert(
                field.to_owned(),
                JsonValue::String(salt.expose().to_owned()),
            );
        }
    }

    /// Take the HTTP client from `clients` instead of building one: every
    /// provider of that holder with the same timeouts and default headers
    /// then uses the same client.
    pub fn shared_clients(mut self, clients: HttpClients) -> Self {
        self.clients = Some(clients);
        self
    }

    /// The HTTP client for an adapter: its default headers, and its read
    /// timeout unless [`RequestOptions::timeout`] sets one.
    pub(crate) fn client(
        &self,
        connect: Duration,
        read: Duration,
    ) -> reqwest::Result<SharedClient> {
        let read = self.timeout.unwrap_or(read);
        let build = || crate::transport::client(connect, read, &self.headers);
        match &self.clients {
            Some(clients) => clients.get(connect, read, &self.headers, build),
            None => build().map(SharedClient::new),
        }
    }
}

/// Merge `extra` into `body`: each field of `extra` replaces the field of
/// the same name, except [`PROTECTED_FIELDS`].
///
/// Two objects are merged instead of replaced. In `chat_template_kwargs`
/// `extra` wins per field; in Gemini's `generationConfig` the adapter's
/// fields win, so the request's temperature, token limit and stop
/// sequences stay, and `extra` adds the vendor's others (`topK`).
pub(crate) fn merge(body: &mut JsonObject, extra: &JsonObject) {
    for (name, value) in extra {
        if PROTECTED_FIELDS.contains(&name.as_str()) {
            continue;
        }
        if name == GENERATION_CONFIG
            && let Some(have) = body.get_mut(name)
        {
            if let (JsonValue::Object(have), JsonValue::Object(add)) = (have, value) {
                for (field, value) in add {
                    have.entry(field.clone()).or_insert_with(|| value.clone());
                }
            }
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
        for name in [
            "Host",
            "content-length",
            "Transfer-Encoding",
            "connection",
            "Proxy-Authorization",
            "proxy-connection",
        ] {
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

    #[test]
    fn api_key_is_a_key_header_in_any_case() {
        for name in ["authorization", "X-Api-Key", "x-goog-api-key", "API-KEY"] {
            assert!(is_key_header(name), "{name}");
        }
        assert!(!is_key_header("x-gateway-token"));
        let options = RequestOptions::default().header("Api-Key", "k").unwrap();
        assert!(options.sets_key_header());
    }

    #[test]
    fn a_credential_named_header_is_scrubbed_at_any_length() {
        let options = RequestOptions::default()
            .header("x-gateway-token", "gw-123")
            .unwrap()
            .header("cookie", "s=1")
            .unwrap()
            .header("x-tenant", "acme")
            .unwrap();
        assert_eq!(
            options.scrub("bad gw-123 and s=1 for acme"),
            "bad [redacted] and [redacted] for acme"
        );
    }

    #[test]
    fn scrub_removes_long_header_values_and_keeps_short_ones() {
        let options = RequestOptions::default()
            .header("x-gateway-token", "gw-secret-value")
            .unwrap()
            .header("x-tenant", "acme")
            .unwrap();
        assert_eq!(
            options.scrub("bad token gw-secret-value for acme"),
            "bad token [redacted] for acme"
        );
        let err = options.scrub_error(ProviderError::Transport {
            message: "echo gw-secret-value".into(),
        });
        assert!(!format!("{err:?}").contains("gw-secret-value"));
    }
}

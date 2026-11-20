//! What `Serialize` writes in place of a config's secrets, and the wire
//! form that keeps them.

use std::collections::BTreeMap;

use linch_core::JsonValue;
use serde::Serializer;
use url::Url;

use super::{AgentConfig, McpTransportConfig, ProviderConfig, StoreConfig};

/// Written in place of an env or header value, an argument, or a DSN or
/// URL that is not a URL.
pub(super) const REDACTED: &str = "[redacted]";

/// Written in place of a password, userinfo or query inside a URL.
/// `[redacted]` would be percent-encoded there.
const REDACTED_IN_URL: &str = "redacted";

/// Serialize `dsn` with its password replaced, in the userinfo and in a
/// `password` query parameter. A DSN that is not a URL is replaced whole.
pub(super) fn dsn<S: Serializer>(dsn: &str, serializer: S) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(&redact_dsn(dsn))
}

/// Serialize `map` with every value replaced; the names stay.
pub(super) fn values<S: Serializer>(
    map: &BTreeMap<String, String>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    serializer.collect_map(map.keys().map(|key| (key, REDACTED)))
}

/// Serialize `args` with each one replaced; their count stays.
pub(super) fn args<S: Serializer>(args: &[String], serializer: S) -> Result<S::Ok, S::Error> {
    serializer.collect_seq(args.iter().map(|_| REDACTED))
}

/// Serialize `url` as [`redact_url`] writes it.
pub(super) fn url<S: Serializer>(url: &str, serializer: S) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(&redact_url(url))
}

/// Serialize an optional `url` as [`redact_url`] writes it.
pub(super) fn url_option<S: Serializer>(
    url: &Option<String>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    match url {
        Some(url) => serializer.serialize_str(&redact_url(url)),
        None => serializer.serialize_none(),
    }
}

/// `url` with its user name, password and query replaced; the scheme,
/// host, port and path stay. A URL without any of the three is returned as
/// given, and one that does not parse is replaced whole.
pub(super) fn redact_url(url: &str) -> String {
    let Ok(mut parsed) = Url::parse(url) else {
        return REDACTED.into();
    };
    if parsed.username().is_empty() && parsed.password().is_none() && parsed.query().is_none() {
        return url.to_owned();
    }
    let user = parsed.username().is_empty() || parsed.set_username(REDACTED_IN_URL).is_ok();
    let password =
        parsed.password().is_none() || parsed.set_password(Some(REDACTED_IN_URL)).is_ok();
    if !(user && password) {
        return REDACTED.into();
    }
    if parsed.query().is_some() {
        parsed.set_query(Some(REDACTED_IN_URL));
    }
    parsed.into()
}

fn redact_dsn(dsn: &str) -> String {
    let Ok(mut url) = Url::parse(dsn) else {
        return REDACTED.into();
    };
    if url.password().is_some() && url.set_password(Some(REDACTED_IN_URL)).is_err() {
        return REDACTED.into();
    }
    let is_password = |key: &str| key.eq_ignore_ascii_case("password");
    if url.query_pairs().any(|(key, _)| is_password(&key)) {
        let pairs: Vec<(String, String)> = (url.query_pairs())
            .map(|(key, value)| {
                let value = if is_password(&key) {
                    REDACTED_IN_URL.into()
                } else {
                    value.into_owned()
                };
                (key.into_owned(), value)
            })
            .collect();
        url.query_pairs_mut().clear().extend_pairs(pairs);
    }
    url.into()
}

impl AgentConfig {
    /// The config as JSON with its secrets: the DSN as given, MCP env and
    /// header values, stdio args and HTTP URLs. This is what `agent.open`
    /// sends and what decoding an `AgentConfig` reads back; `Serialize`
    /// writes them redacted, for logs and echoes.
    ///
    /// Raises:
    ///     serde_json::Error: If a part of the config does not encode,
    ///         which the config types never cause.
    pub fn to_value_with_secrets(&self) -> Result<JsonValue, serde_json::Error> {
        let mut value = serde_json::to_value(self)?;
        if let StoreConfig::Postgres { dsn, .. } = &self.stores {
            value["stores"]["dsn"] = JsonValue::from(dsn.as_str());
        }
        for (index, server) in self.mcp_servers.iter().enumerate() {
            let transport = &mut value["mcp_servers"][index]["transport"];
            match &server.transport {
                McpTransportConfig::Stdio { args, env, .. } => {
                    transport["args"] = serde_json::to_value(args)?;
                    transport["env"] = serde_json::to_value(env)?;
                }
                McpTransportConfig::Http { url, headers } => {
                    transport["url"] = JsonValue::from(url.as_str());
                    transport["headers"] = serde_json::to_value(headers)?;
                }
            }
        }
        if let Some(url) = provider_base_url(&self.provider) {
            value["provider"]["base_url"] = JsonValue::from(url);
        }
        if let Some(request) = self.provider.request()
            && !request.default_headers.is_empty()
        {
            value["provider"]["request"]["default_headers"] =
                serde_json::to_value(&request.default_headers)?;
        }
        Ok(value)
    }
}

/// The `base_url` a provider config names, if any.
fn provider_base_url(provider: &ProviderConfig) -> Option<&str> {
    match provider {
        ProviderConfig::DeepSeek { base_url, .. }
        | ProviderConfig::OpenAiChat { base_url, .. }
        | ProviderConfig::OpenAiResponses { base_url, .. }
        | ProviderConfig::Anthropic { base_url, .. }
        | ProviderConfig::Gemini { base_url, .. } => base_url.as_deref(),
        ProviderConfig::Vllm { base_url, .. }
        | ProviderConfig::SgLang { base_url, .. }
        | ProviderConfig::LlamaCpp { base_url, .. } => Some(base_url),
        ProviderConfig::Scripted { .. } => None,
    }
}

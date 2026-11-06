//! What `Serialize` writes in place of a config's secrets, and the wire
//! form that keeps them.

use std::collections::BTreeMap;

use linch_core::JsonValue;
use serde::Serializer;
use url::Url;

use super::{AgentConfig, McpTransportConfig, StoreConfig};

/// Written in place of an env or header value, or a DSN that is not a URL.
const REDACTED: &str = "[redacted]";

/// Written in place of a password inside a DSN URL. `[redacted]` would be
/// percent-encoded there.
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
    /// header values. This is what `agent.open` sends and what decoding an
    /// `AgentConfig` reads back; `Serialize` writes them redacted, for logs
    /// and echoes.
    ///
    /// Raises:
    ///     serde_json::Error: If a part of the config does not encode,
    ///         which the config types never cause.
    pub fn to_value_with_secrets(&self) -> Result<JsonValue, serde_json::Error> {
        let mut value = serde_json::to_value(self)?;
        if let StoreConfig::Postgres { dsn } = &self.stores {
            value["stores"]["dsn"] = JsonValue::from(dsn.as_str());
        }
        for (index, server) in self.mcp_servers.iter().enumerate() {
            let (key, secrets) = match &server.transport {
                McpTransportConfig::Stdio { env, .. } => ("env", env),
                McpTransportConfig::Http { headers, .. } => ("headers", headers),
            };
            value["mcp_servers"][index]["transport"][key] = serde_json::to_value(secrets)?;
        }
        Ok(value)
    }
}

//! Params of `agent.open`: an agent config plus what the connection's
//! client serves for it.

use std::num::NonZeroU64;

use linch::{AgentConfig, JsonObject, JsonValue, ToolScope};
use serde::de::{DeserializeOwned, Error as _};
use serde::ser::Error as _;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use super::CallbackSpec;

/// Params of `agent.open`: the members of an [`AgentConfig`] plus the
/// optional `tools` and `callbacks` this connection's client serves.
///
/// Decoding is strict at every level: `tools` and `callbacks` are taken
/// out, and the remaining members must form a valid [`AgentConfig`].
#[derive(Debug, Clone, PartialEq)]
pub struct OpenParams {
    /// The agent config.
    pub config: AgentConfig,
    /// Host tools, registered before the config's workspace and MCP tools.
    pub tools: Vec<HostToolSpec>,
    /// Intercept callbacks, each run in declaration order within its point.
    pub callbacks: Vec<CallbackSpec>,
}

/// A tool implemented by the client and called with `tool.call` on the
/// connection that opened the agent.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostToolSpec {
    /// Name the model calls it by: 1-64 ASCII letters, digits, `_`, `-`.
    pub name: String,
    /// Description shown to the model.
    pub description: String,
    /// JSON Schema (draft 2020-12) of the input; `"type": "object"` at the
    /// top level.
    pub input_schema: JsonObject,
    /// What the tool may touch: `read`, `write` or `exec`. Required, with no
    /// default: the scope decides auto-approval and whether calls may run
    /// in parallel (only `read` ones do).
    pub scope: ToolScope,
    /// Longest wait for one call before it is cancelled (default 300 000).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeout_ms: Option<NonZeroU64>,
}

impl Serialize for OpenParams {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut object = match serde_json::to_value(&self.config).map_err(S::Error::custom)? {
            JsonValue::Object(object) => object,
            _ => return Err(S::Error::custom("an agent config encodes as an object")),
        };
        put(&mut object, "tools", &self.tools).map_err(S::Error::custom)?;
        put(&mut object, "callbacks", &self.callbacks).map_err(S::Error::custom)?;
        object.serialize(serializer)
    }
}

impl<'de> Deserialize<'de> for OpenParams {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let mut object = JsonObject::deserialize(deserializer)?;
        let tools = take(&mut object, "tools")?;
        let callbacks = take(&mut object, "callbacks")?;
        let config = serde_json::from_value(JsonValue::Object(object)).map_err(D::Error::custom)?;
        Ok(Self {
            config,
            tools,
            callbacks,
        })
    }
}

/// Insert `items` under `key` unless empty.
fn put<T: Serialize>(
    object: &mut JsonObject,
    key: &str,
    items: &[T],
) -> Result<(), serde_json::Error> {
    if !items.is_empty() {
        object.insert(key.into(), serde_json::to_value(items)?);
    }
    Ok(())
}

/// Remove `key` from `object` and decode it; absent is empty.
fn take<T: DeserializeOwned, E: serde::de::Error>(
    object: &mut JsonObject,
    key: &str,
) -> Result<Vec<T>, E> {
    match object.remove(key) {
        None => Ok(Vec::new()),
        Some(value) => {
            serde_json::from_value(value).map_err(|error| E::custom(format!("{key}: {error}")))
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn open_params_round_trip_and_stay_strict() {
        let wire = json!({
            "namespace": "t",
            "spec": {"model": "m"},
            "provider": {"id": "deepseek"},
            "tools": [{"name": "lookup", "description": "Find.",
                       "input_schema": {"type": "object"}, "scope": "read",
                       "timeout_ms": 5}],
            "callbacks": [{"point": "stop", "policy_id": "p", "policy_version": 2}],
        });
        let params: OpenParams = serde_json::from_value(wire.clone()).unwrap();
        assert_eq!(params.tools[0].scope, ToolScope::Read);
        assert_eq!(params.callbacks[0].policy_version, 2);
        let encoded = serde_json::to_value(&params).unwrap();
        assert_eq!(encoded["tools"], wire["tools"]);
        assert_eq!(encoded["callbacks"], wire["callbacks"]);
        assert_eq!(
            serde_json::from_value::<OpenParams>(encoded).unwrap(),
            params
        );

        let bare = json!({"namespace": "t", "spec": {"model": "m"},
                          "provider": {"id": "deepseek"}});
        let params: OpenParams = serde_json::from_value(bare.clone()).unwrap();
        assert!(params.tools.is_empty() && params.callbacks.is_empty());
        let encoded = serde_json::to_value(&params).unwrap();
        assert!(encoded.get("tools").is_none() && encoded.get("callbacks").is_none());

        let mut unknown = bare;
        unknown["hooks"] = json!([]);
        assert!(serde_json::from_value::<OpenParams>(unknown).is_err());
    }
}

//! The normalized request sent to a provider for one call.

use serde::{Deserialize, Serialize};

use crate::json::JsonObject;
use crate::message::{Message, SystemBlock};

/// Collects the normalized inputs for one provider call.
///
/// `model`, `system`, `tools`, and `messages` are always required; every
/// other field is an optional control a provider may ignore or downgrade.
/// Host-only concerns from the Python reference — a cancellation signal,
/// retry budget, and the native-catalog bookkeeping flag — have no place
/// here and are not ported.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderRequest {
    /// Model identifier requested for the call.
    pub model: String,
    /// System-prompt blocks, in provider order.
    pub system: Vec<SystemBlock>,
    /// Tool schemas offered to the model.
    pub tools: Vec<ToolDefinition>,
    /// Conversation messages included in the request.
    pub messages: Vec<Message>,
    /// Maximum tokens the provider may generate.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_output_tokens: Option<u32>,
    /// Sampling temperature.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f64>,
    /// Sequences that end generation when produced.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub stop_sequences: Vec<String>,
    /// Requested reasoning effort.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effort: Option<ReasoningEffort>,
    /// Token budget for extended thinking/reasoning.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thinking_budget: Option<u32>,
    /// Requested prompt-cache behavior.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cache_control: Option<CacheControl>,
    /// Schema the response must satisfy, for structured output.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_schema: Option<OutputSchema>,
    /// Policy for whether and which tool the model must call.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_choice: Option<ToolChoice>,
}

/// One tool schema offered to the model.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolDefinition {
    /// Name the model uses to call the tool.
    pub name: String,
    /// Description shown to the model.
    pub description: String,
    /// JSON Schema describing accepted input.
    pub input_schema: JsonObject,
}

/// JSON Schema specification for constrained structured output.
///
/// A provider that supports structured output constrains its response to
/// this schema; otherwise the caller must parse and validate the returned
/// text itself.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputSchema {
    /// Schema identifier sent to the provider.
    pub name: String,
    /// JSON Schema describing the output shape.
    pub schema: JsonObject,
    /// Whether provider-side strict schema validation is requested.
    #[serde(default = "default_strict")]
    pub strict: bool,
    /// Human-readable description of the schema.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
}

fn default_strict() -> bool {
    true
}

/// Reasoning effort requested for a call, for providers that support it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReasoningEffort {
    /// Minimal reasoning.
    Low,
    /// Moderate reasoning.
    Medium,
    /// Extensive reasoning.
    High,
    /// Very extensive reasoning.
    Xhigh,
    /// Maximum available reasoning.
    Max,
}

/// Requested provider prompt-cache behavior for a request.
///
/// `None` means caching is not requested; `Some` requests it, optionally
/// with an explicit lifetime.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CacheControl {
    /// Requested cache lifetime; `None` uses the provider's default.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ttl: Option<CacheTtl>,
}

/// Requested prompt-cache lifetime.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum CacheTtl {
    /// A five-minute cache lifetime.
    #[serde(rename = "5m")]
    FiveMinutes,
    /// A one-hour cache lifetime.
    #[serde(rename = "1h")]
    OneHour,
}

/// Which tool, if any, the model must call.
///
/// `Auto`, `Disabled`, and `Required` are written as empty struct variants
/// (`Auto {}`, not a unit variant) so `deny_unknown_fields` rejects a stray
/// field alongside their tag; serde does not enforce that for a bare unit
/// variant of an internally tagged enum. The wire shape is unaffected: an
/// empty struct variant still serializes as `{"type": "auto"}`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum ToolChoice {
    /// The model chooses freely among the offered tools, or none.
    Auto {},
    /// The model must not call any tool.
    #[serde(rename = "none")]
    Disabled {},
    /// The model must call some tool.
    Required {},
    /// The model must call the named tool.
    Tool {
        /// Name of the tool the model must call.
        name: String,
    },
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn minimal_request() -> ProviderRequest {
        ProviderRequest {
            model: "m".into(),
            system: vec![],
            tools: vec![],
            messages: vec![],
            max_output_tokens: None,
            temperature: None,
            stop_sequences: vec![],
            effort: None,
            thinking_budget: None,
            cache_control: None,
            output_schema: None,
            tool_choice: None,
        }
    }

    #[test]
    fn absent_controls_are_omitted_on_the_wire() {
        let wire = serde_json::to_value(minimal_request()).unwrap();
        assert_eq!(
            wire,
            json!({"model": "m", "system": [], "tools": [], "messages": []})
        );
        assert_eq!(
            serde_json::from_value::<ProviderRequest>(wire).unwrap(),
            minimal_request()
        );
    }

    #[test]
    fn unknown_request_fields_are_rejected() {
        let mut wire = serde_json::to_value(minimal_request()).unwrap();
        wire.as_object_mut()
            .unwrap()
            .insert("signal".into(), json!(null));
        assert!(serde_json::from_value::<ProviderRequest>(wire).is_err());
    }

    #[test]
    fn output_schema_strict_defaults_true_and_always_serializes() {
        let schema = OutputSchema {
            name: "s".into(),
            schema: serde_json::Map::new(),
            strict: true,
            description: None,
        };
        let wire = serde_json::to_value(&schema).unwrap();
        assert_eq!(wire, json!({"name": "s", "schema": {}, "strict": true}));
        let parsed: OutputSchema =
            serde_json::from_value(json!({"name": "s", "schema": {}})).unwrap();
        assert!(parsed.strict);
    }

    #[test]
    fn tool_choice_variants_round_trip() {
        for (choice, wire) in [
            (ToolChoice::Auto {}, json!({"type": "auto"})),
            (ToolChoice::Disabled {}, json!({"type": "none"})),
            (ToolChoice::Required {}, json!({"type": "required"})),
            (
                ToolChoice::Tool {
                    name: "Read".into(),
                },
                json!({"type": "tool", "name": "Read"}),
            ),
        ] {
            assert_eq!(serde_json::to_value(&choice).unwrap(), wire);
            assert_eq!(serde_json::from_value::<ToolChoice>(wire).unwrap(), choice);
        }
        assert!(serde_json::from_value::<ToolChoice>(json!({"type": "auto", "extra": 1})).is_err());
    }

    #[test]
    fn cache_ttl_uses_short_wire_names() {
        assert_eq!(
            serde_json::to_value(CacheTtl::FiveMinutes).unwrap(),
            json!("5m")
        );
        assert_eq!(
            serde_json::to_value(CacheTtl::OneHour).unwrap(),
            json!("1h")
        );
    }
}

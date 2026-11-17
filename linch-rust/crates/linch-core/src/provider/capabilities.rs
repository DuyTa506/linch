//! What a provider supports ([`ProviderCapabilities`]) and what its server
//! lists about its models ([`ModelInfo`]), as plain data.
//!
//! Both are answers an adapter gives about itself: [`crate::Provider::capabilities`]
//! never does I/O, and the model calls ask the vendor's API.

use serde::{Deserialize, Serialize};
use thiserror::Error;

use super::{ProviderError, Thinking, ToolChoice};

/// Whether a provider takes something, when the answer can depend on the
/// model served.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Support {
    /// Every model it serves does.
    Supported,
    /// None does: the adapter refuses or ignores it.
    Unsupported,
    /// The adapter sends it, and whether the model takes it is the model's
    /// own matter (a self-hosted server serves whatever model it was
    /// started with).
    ModelDependent,
    /// The adapter declares nothing about it.
    Unknown,
}

/// Where [`ProviderCapabilities::context_window`] comes from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ContextSource {
    /// The host set it for the provider.
    Configured,
    /// The server reported it (`max_model_len`, `n_ctx`).
    Server,
    /// The adapter's own table of models.
    Table,
    /// None of the above: the adapter's default, a guess.
    Default,
}

/// A state of a request's `thinking`, as far as a forced tool choice cares.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ThinkingState {
    /// No `thinking` in the request: the provider's own default.
    Unset,
    /// `adaptive`, or `enabled` with no budget.
    Adaptive,
    /// `enabled` with a token budget.
    Budgeted,
}

/// Which `tool_choice` forms a provider takes.
///
/// Read from the provider's own [`crate::Provider::tool_choice_refusal`] by
/// [`ToolChoiceCaps::probe`], so it cannot differ from what the agent
/// refuses when it is built.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolChoiceCaps {
    /// `auto`.
    pub auto: bool,
    /// `none`.
    pub none: bool,
    /// `required`, with thinking off.
    pub required: bool,
    /// A named tool, with thinking off.
    pub named: bool,
    /// The thinking states in which `required` and a named tool are
    /// refused although thinking off takes them; empty when thinking never
    /// matters. `auto` and `none` are taken in every state.
    pub forced_refused_with: Vec<ThinkingState>,
}

impl ToolChoiceCaps {
    /// Ask `refusal` (a provider's [`crate::Provider::tool_choice_refusal`]) about
    /// every form, with each thinking state.
    pub fn probe(refusal: impl Fn(&ToolChoice, Option<&Thinking>) -> Option<String>) -> Self {
        let off = Thinking::Disabled {};
        let takes =
            |choice: &ToolChoice, thinking: Option<&Thinking>| refusal(choice, thinking).is_none();
        let named = ToolChoice::Tool {
            name: "probe".into(),
        };
        let (required, named_ok) = (
            takes(&ToolChoice::Required {}, Some(&off)),
            takes(&named, Some(&off)),
        );
        // A form the provider never takes is not listed under a state.
        let refused_in = |thinking: Option<&Thinking>| {
            (required && !takes(&ToolChoice::Required {}, thinking))
                || (named_ok && !takes(&named, thinking))
        };
        let adaptive = Thinking::Adaptive { display: None };
        let budgeted = Thinking::Enabled {
            budget_tokens: Some(1024),
            display: None,
        };
        let forced_refused_with = [
            (ThinkingState::Unset, None),
            (ThinkingState::Adaptive, Some(&adaptive)),
            (ThinkingState::Budgeted, Some(&budgeted)),
        ]
        .into_iter()
        .filter(|(_, thinking)| refused_in(*thinking))
        .map(|(state, _)| state)
        .collect();
        Self {
            auto: takes(&ToolChoice::Auto {}, Some(&off)),
            none: takes(&ToolChoice::Disabled {}, Some(&off)),
            required,
            named: named_ok,
            forced_refused_with,
        }
    }
}

/// What the request's reasoning controls do.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThinkingCaps {
    /// `thinking: disabled` turns reasoning off.
    pub disable: bool,
    /// `thinking: enabled` with `budget_tokens` bounds it.
    pub budget: bool,
    /// `effort` is sent.
    pub effort: bool,
    /// The stream carries the model's reasoning text.
    pub reasoning_text: bool,
}

/// How a provider caches a prompt prefix.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PromptCache {
    /// None the adapter knows of.
    None,
    /// The server caches a shared prefix by itself; a request's
    /// `cache_control` is not sent.
    Automatic,
    /// A request's `cache_control` marks what is cached.
    Explicit,
}

/// What one provider adapter supports for one model.
///
/// Each adapter declares its own; none takes another's. A provider that
/// declares nothing ([`crate::Provider::capabilities`]'s default) has
/// `declared` false and every other field at its most cautious value.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderCapabilities {
    /// [`crate::Provider::id`].
    pub provider: String,
    /// The model asked about.
    pub model: String,
    /// Whether the adapter declares the fields below the next one; false
    /// for a provider that only answers the generic questions.
    pub declared: bool,
    /// The most tokens a request takes, prompt and output together, as a
    /// run would use it.
    pub context_window: u64,
    /// Where `context_window` comes from; [`ContextSource::Default`] is a
    /// guess, not something the server told.
    pub context_source: ContextSource,
    /// The run's output schema goes in the request; false: a run offers a
    /// final tool instead ([`crate::Provider::native_output_schema`]).
    pub native_output_schema: bool,
    /// The `tool_choice` forms taken.
    pub tool_choice: ToolChoiceCaps,
    /// The reasoning controls.
    pub thinking: ThinkingCaps,
    /// Image input.
    pub vision: Support,
    /// A tool call's arguments stream in pieces; false: they arrive whole.
    pub streams_tool_arguments: bool,
    /// Prompt caching.
    pub prompt_cache: PromptCache,
    /// `stop_sequences` are sent; false: the adapter refuses them.
    pub stop_sequences: bool,
}

impl ProviderCapabilities {
    /// The answer of a provider that declares nothing beyond what the
    /// [`crate::Provider`] trait already says: its id, its window for `model`,
    /// whether it takes an output schema, and its tool choice rule.
    pub fn undeclared(
        provider: &str,
        model: &str,
        context_window: u64,
        native_output_schema: bool,
        tool_choice: ToolChoiceCaps,
    ) -> Self {
        Self {
            provider: provider.to_owned(),
            model: model.to_owned(),
            declared: false,
            context_window,
            context_source: ContextSource::Default,
            native_output_schema,
            tool_choice,
            thinking: ThinkingCaps {
                disable: false,
                budget: false,
                effort: false,
                reasoning_text: false,
            },
            vision: Support::Unknown,
            streams_tool_arguments: false,
            prompt_cache: PromptCache::None,
            stop_sequences: false,
        }
    }
}

/// One model a provider's server lists.
///
/// Only what the vendor's API gives: a field the listing leaves out is
/// `None` or empty, never a guess.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelInfo {
    /// The id a request names the model by.
    pub id: String,
    /// A human-readable name.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    /// The most input tokens it takes (`max_model_len`, `inputTokenLimit`,
    /// `max_input_tokens`, `context_window`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_window: Option<u64>,
    /// The most tokens it generates in one answer.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_output_tokens: Option<u64>,
    /// What it takes as input (`text`, `image`), when the listing says.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub input_modalities: Vec<String>,
}

impl ModelInfo {
    /// A model with only its id.
    pub fn new(id: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            display_name: None,
            context_window: None,
            max_output_tokens: None,
            input_modalities: Vec::new(),
        }
    }
}

/// Why [`crate::Provider::list_models`] or [`crate::Provider::model_info`] gave no model.
///
/// Messages never carry the key or the endpoint's URL.
#[derive(Debug, Error)]
pub enum ModelsError {
    /// The provider has no model list to ask: it has no such API, or the
    /// adapter does not use it.
    #[error("provider {provider:?} does not list its models")]
    Unsupported {
        /// [`crate::Provider::id`].
        provider: String,
    },
    /// The list does not hold the model.
    #[error("the provider lists no model {model:?}")]
    NotFound {
        /// The id asked for.
        model: String,
    },
    /// The request failed or the answer was not a model list.
    #[error(transparent)]
    Provider(#[from] ProviderError),
}

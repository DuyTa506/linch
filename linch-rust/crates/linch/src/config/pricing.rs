//! `AgentConfig.pricing`: rates a config adds or replaces for its own agent.

use std::collections::BTreeMap;

use linch_core::Usage;
use linch_providers::ModelPricing;
use serde::{Deserialize, Serialize};

/// USD per million tokens for one model, as a config states it.
///
/// `input` and `output` are required. `cache_read` and `cache_creation`
/// default to `input`: a cached token costs what an uncached one does
/// unless the config says less. Every rate must be finite and not
/// negative; decoding rejects anything else and unknown fields.
///
/// ```json
/// {"input": 0.2, "output": 0.6, "cache_read": 0.05}
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(try_from = "RawRates")]
pub struct ModelRates {
    /// Uncached input tokens.
    pub input: f64,
    /// Generated output tokens.
    pub output: f64,
    /// Input tokens served from the prompt cache.
    pub cache_read: f64,
    /// Input tokens written to the prompt cache.
    pub cache_creation: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawRates {
    input: f64,
    output: f64,
    #[serde(default)]
    cache_read: Option<f64>,
    #[serde(default)]
    cache_creation: Option<f64>,
}

impl TryFrom<RawRates> for ModelRates {
    type Error = String;

    fn try_from(raw: RawRates) -> Result<Self, String> {
        let rates = Self {
            input: raw.input,
            output: raw.output,
            cache_read: raw.cache_read.unwrap_or(raw.input),
            cache_creation: raw.cache_creation.unwrap_or(raw.input),
        };
        let all = [
            ("input", rates.input),
            ("output", rates.output),
            ("cache_read", rates.cache_read),
            ("cache_creation", rates.cache_creation),
        ];
        match all
            .iter()
            .find(|(_, rate)| !rate.is_finite() || *rate < 0.0)
        {
            Some((name, rate)) => Err(format!(
                "{name} must be a finite rate of at least 0, got {rate}"
            )),
            None => Ok(rates),
        }
    }
}

impl From<ModelRates> for ModelPricing {
    fn from(rates: ModelRates) -> Self {
        Self {
            input: rates.input,
            output: rates.output,
            cache_read: rates.cache_read,
            cache_creation: rates.cache_creation,
        }
    }
}

/// Rates by exact model id.
pub type PricingOverrides = BTreeMap<String, ModelRates>;

/// Reject an empty model id, which no call can name.
pub(super) fn check<'de, D>(deserializer: D) -> Result<PricingOverrides, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let map = PricingOverrides::deserialize(deserializer)?;
    match map.contains_key("") {
        true => Err(serde::de::Error::custom("pricing: a model id is empty")),
        false => Ok(map),
    }
}

/// The agent's pricing: the override of a model when the config has one,
/// else the published table, so a model neither knows has no cost.
pub(crate) fn pricing(
    overrides: PricingOverrides,
) -> impl Fn(&str, &Usage) -> Option<f64> + Send + Sync + 'static {
    move |model, usage| match overrides.get(model) {
        Some(rates) => Some(ModelPricing::from(*rates).cost(usage)),
        None => linch_providers::cost(model, usage),
    }
}

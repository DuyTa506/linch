//! Token counts to USD cost, ported from Python's `linch.pricing`.
//!
//! Rates are USD per million tokens, sourced 2026-06-08 from
//! <https://www.anthropic.com/pricing>. Cache write rates are the 5-minute
//! TTL tier.

use linch_core::Usage;

const PER_MILLION: f64 = 1_000_000.0;

/// USD-per-million-token rates for one model.
///
/// The published rates come from [`model_pricing`]. A caller with other
/// rates, such as the 1-hour cache TTL (twice the 5-minute creation rate),
/// builds its own and prices with [`ModelPricing::cost`].
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ModelPricing {
    /// Uncached input tokens.
    pub input: f64,
    /// Generated output tokens.
    pub output: f64,
    /// Input tokens served from the prompt cache.
    pub cache_read: f64,
    /// Input tokens written to the prompt cache.
    pub cache_creation: f64,
}

impl ModelPricing {
    /// The USD cost of `usage` at these rates.
    ///
    /// The four buckets are priced and summed independently: a provider
    /// reports cache reads and writes apart from `input_tokens`.
    pub fn cost(&self, usage: &Usage) -> f64 {
        usage.input_tokens as f64 * self.input / PER_MILLION
            + usage.output_tokens as f64 * self.output / PER_MILLION
            + usage.cache_read_tokens as f64 * self.cache_read / PER_MILLION
            + usage.cache_creation_tokens as f64 * self.cache_creation / PER_MILLION
    }
}

const fn rates(input: f64, output: f64, cache_read: f64, cache_creation: f64) -> ModelPricing {
    ModelPricing {
        input,
        output,
        cache_read,
        cache_creation,
    }
}

// Cache reads are about 0.1x input, cache writes about 1.25x.
const OPUS_4: ModelPricing = rates(5.00, 25.00, 0.50, 6.25);
const SONNET_4: ModelPricing = rates(3.00, 15.00, 0.30, 3.75);
const HAIKU_4: ModelPricing = rates(1.00, 5.00, 0.10, 1.25);

/// Published rates by exact model id.
const PRICING: &[(&str, ModelPricing)] = &[
    ("claude-opus-4-8", OPUS_4),
    ("claude-opus-4-7", OPUS_4),
    ("claude-opus-4-6", OPUS_4),
    ("claude-sonnet-4-6", SONNET_4),
    ("claude-haiku-4-5", HAIKU_4),
    ("claude-haiku-4-5-20251001", HAIKU_4),
];

/// The published rates for `model`, matched exactly on its id.
///
/// Rates are USD per million tokens, sourced 2026-06-08 from
/// <https://www.anthropic.com/pricing>, with cache writes at the 5-minute
/// TTL tier.
///
/// Returns `None` for a model the table does not list.
pub fn model_pricing(model: &str) -> Option<ModelPricing> {
    PRICING
        .iter()
        .find(|(id, _)| *id == model)
        .map(|(_, pricing)| *pricing)
}

/// The USD cost of `usage` on `model`, at the rates of [`model_pricing`].
///
/// An unknown model has no cost: `None`, never a silent zero. Unknown usage
/// is `Option<Usage>::None` at the caller and stays `None` through
/// `and_then`.
///
/// ```
/// use linch_core::Usage;
/// use linch_providers::cost;
///
/// let usage = Usage { input_tokens: 1_000_000, ..Usage::default() };
/// assert_eq!(cost("claude-opus-4-8", &usage), Some(5.0));
/// assert_eq!(cost("some-other-model", &usage), None);
/// let unknown: Option<Usage> = None;
/// assert_eq!(unknown.and_then(|usage| cost("claude-opus-4-8", &usage)), None);
/// ```
///
/// Returns the cost in USD, or `None` when the model is not in the table.
pub fn cost(model: &str, usage: &Usage) -> Option<f64> {
    model_pricing(model).map(|pricing| pricing.cost(usage))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pricing(input: f64, output: f64, cache_read: f64, cache_creation: f64) -> ModelPricing {
        ModelPricing {
            input,
            output,
            cache_read,
            cache_creation,
        }
    }

    #[test]
    fn the_table_matches_python() {
        let opus = pricing(5.00, 25.00, 0.50, 6.25);
        let sonnet = pricing(3.00, 15.00, 0.30, 3.75);
        let haiku = pricing(1.00, 5.00, 0.10, 1.25);
        let python = [
            ("claude-opus-4-8", opus),
            ("claude-opus-4-7", opus),
            ("claude-opus-4-6", opus),
            ("claude-sonnet-4-6", sonnet),
            ("claude-haiku-4-5", haiku),
            ("claude-haiku-4-5-20251001", haiku),
        ];
        for (model, rates) in python {
            assert_eq!(model_pricing(model), Some(rates), "{model}");
        }
        assert_eq!(PRICING.len(), python.len());
    }

    #[test]
    fn every_bucket_is_priced_at_its_own_rate() {
        let million = 1_000_000;
        let only = |usage: Usage| cost("claude-sonnet-4-6", &usage);
        let zero = Usage::default();
        for (usage, usd) in [
            (
                Usage {
                    input_tokens: million,
                    ..zero
                },
                3.00,
            ),
            (
                Usage {
                    output_tokens: million,
                    ..zero
                },
                15.00,
            ),
            (
                Usage {
                    cache_read_tokens: million,
                    ..zero
                },
                0.30,
            ),
            (
                Usage {
                    cache_creation_tokens: million,
                    ..zero
                },
                3.75,
            ),
        ] {
            assert_eq!(only(usage), Some(usd), "{usage:?}");
        }
        let all = Usage {
            input_tokens: million,
            output_tokens: million,
            cache_read_tokens: million,
            cache_creation_tokens: million,
        };
        assert_eq!(only(all), Some(22.05));
    }

    /// The same sum in the same order as Python, so the float is identical.
    #[test]
    fn a_mixed_usage_costs_what_python_computes() {
        let usage = Usage {
            input_tokens: 1234,
            output_tokens: 567,
            cache_read_tokens: 8901,
            cache_creation_tokens: 234,
        };
        assert_eq!(cost("claude-sonnet-4-6", &usage), Some(0.0157548));
    }

    #[test]
    fn an_unknown_model_has_no_cost_and_a_known_one_can_cost_zero() {
        let usage = Usage {
            input_tokens: 10,
            ..Usage::default()
        };
        for model in ["gpt-5.5", "claude-sonnet-4-6-x", "Claude-Sonnet-4-6", ""] {
            assert_eq!(cost(model, &usage), None, "{model:?}");
        }
        assert_eq!(cost("claude-haiku-4-5", &Usage::default()), Some(0.0));
    }
}

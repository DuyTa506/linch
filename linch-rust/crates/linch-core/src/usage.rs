//! Token usage reported by a provider call.

use serde::{Deserialize, Serialize};
use std::ops::{Add, AddAssign};

/// Tokens reported for one provider call, or a sum of calls.
///
/// Cache reads and cache writes are reported separately and are not part of
/// `input_tokens`. A call whose usage is unknown is represented by the caller
/// as `Option<Usage>::None`, never as a zero `Usage`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Usage {
    /// Uncached input tokens.
    #[serde(default)]
    pub input_tokens: u64,
    /// Generated output tokens.
    #[serde(default)]
    pub output_tokens: u64,
    /// Input tokens served from the provider's prompt cache.
    #[serde(default)]
    pub cache_read_tokens: u64,
    /// Input tokens written to the provider's prompt cache.
    #[serde(default)]
    pub cache_creation_tokens: u64,
}

impl Usage {
    /// Every input bucket: uncached, cache reads and cache writes.
    pub fn total_input_tokens(&self) -> u64 {
        self.input_tokens
            .saturating_add(self.cache_read_tokens)
            .saturating_add(self.cache_creation_tokens)
    }
}

impl Add for Usage {
    type Output = Usage;

    fn add(self, other: Usage) -> Usage {
        Usage {
            input_tokens: self.input_tokens.saturating_add(other.input_tokens),
            output_tokens: self.output_tokens.saturating_add(other.output_tokens),
            cache_read_tokens: self
                .cache_read_tokens
                .saturating_add(other.cache_read_tokens),
            cache_creation_tokens: self
                .cache_creation_tokens
                .saturating_add(other.cache_creation_tokens),
        }
    }
}

impl AddAssign for Usage {
    fn add_assign(&mut self, other: Usage) {
        *self = *self + other;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn missing_buckets_default_to_zero_and_unknown_fields_fail() {
        let usage: Usage = serde_json::from_str(r#"{"input_tokens": 3}"#).unwrap();
        assert_eq!(
            usage,
            Usage {
                input_tokens: 3,
                ..Usage::default()
            }
        );
        assert!(serde_json::from_str::<Usage>(r#"{"tokens": 3}"#).is_err());
        assert!(serde_json::from_str::<Usage>(r#"{"input_tokens": -1}"#).is_err());
    }

    proptest! {
        #[test]
        fn addition_is_fieldwise_and_saturating(a in any::<[u64; 4]>(), b in any::<[u64; 4]>()) {
            let left = Usage { input_tokens: a[0], output_tokens: a[1], cache_read_tokens: a[2], cache_creation_tokens: a[3] };
            let right = Usage { input_tokens: b[0], output_tokens: b[1], cache_read_tokens: b[2], cache_creation_tokens: b[3] };
            let sum = left + right;
            prop_assert_eq!(sum.input_tokens, a[0].saturating_add(b[0]));
            prop_assert_eq!(sum.output_tokens, a[1].saturating_add(b[1]));
            prop_assert_eq!(sum.cache_read_tokens, a[2].saturating_add(b[2]));
            prop_assert_eq!(sum.cache_creation_tokens, a[3].saturating_add(b[3]));
            prop_assert_eq!(sum, right + left);
        }
    }
}

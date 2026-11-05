//! Cross-cutting properties over the whole module: key-order independence,
//! the parse/canonicalize round trip, and idempotence.

use proptest::prelude::*;
use serde_json::json;

use crate::json::JsonValue;

use super::to_canonical_string;

/// Any JSON value with finite floats. `serde_json::Value` cannot hold
/// NaN or infinity, and `float_roundtrip` makes every finite float stable.
fn arbitrary_json() -> impl Strategy<Value = JsonValue> {
    let leaf = prop_oneof![
        Just(JsonValue::Null),
        any::<bool>().prop_map(JsonValue::Bool),
        any::<i64>().prop_map(|n| json!(n)),
        any::<f64>()
            .prop_filter("finite", |f| f.is_finite())
            .prop_map(|f| json!(f)),
        ".*".prop_map(JsonValue::String),
    ];
    leaf.prop_recursive(6, 64, 8, |inner| {
        prop_oneof![
            prop::collection::vec(inner.clone(), 0..4).prop_map(JsonValue::Array),
            prop::collection::btree_map(".*", inner, 0..4).prop_map(|m| json!(m)),
        ]
    })
}

/// Reverses every object's own entry order (recursively) — a permutation of
/// the original tree's insertion order that must canonicalize identically.
fn reversed(value: &JsonValue) -> JsonValue {
    match value {
        JsonValue::Array(items) => JsonValue::Array(items.iter().map(reversed).collect()),
        JsonValue::Object(map) => {
            let mut entries: Vec<(String, JsonValue)> =
                map.iter().map(|(k, v)| (k.clone(), reversed(v))).collect();
            entries.reverse();
            JsonValue::Object(entries.into_iter().collect())
        }
        other => other.clone(),
    }
}

proptest! {
    #[test]
    fn canonical_form_is_independent_of_key_order(value in arbitrary_json()) {
        prop_assert_eq!(to_canonical_string(&value), to_canonical_string(&reversed(&value)));
    }

    #[test]
    fn reparsing_the_canonical_string_yields_an_equal_value(value in arbitrary_json()) {
        let text = to_canonical_string(&value);
        let reparsed: JsonValue = serde_json::from_str(&text).expect("canonical output is valid JSON");
        prop_assert_eq!(reparsed, value);
    }

    #[test]
    fn canonicalizing_is_idempotent(value in arbitrary_json()) {
        let once = to_canonical_string(&value);
        let reparsed: JsonValue = serde_json::from_str(&once).unwrap();
        let twice = to_canonical_string(&reparsed);
        prop_assert_eq!(once, twice);
    }
}

#[test]
fn extreme_floats_round_trip_bit_exactly() {
    // The default serde_json parser lands one ULP off here.
    let f = -2.074_045_307_101_22e269_f64;
    let back: f64 = serde_json::from_str(&to_canonical_string(&json!(f))).unwrap();
    assert_eq!(back.to_bits(), f.to_bits());
}

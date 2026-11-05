//! Cross-cutting properties over the whole module: key-order independence,
//! the parse/canonicalize round trip, and idempotence.

use proptest::prelude::*;
use serde_json::json;

use crate::json::JsonValue;

use super::to_canonical_string;

/// Whether `f` survives `serde_json`'s own format-then-parse round trip.
///
/// `to_canonical_string` delegates number formatting entirely to
/// `serde_json` (see the `canonical` module docs); this crate makes no
/// promise beyond what `serde_json` itself promises. At extreme magnitudes
/// (observed around `1e269`) this build of `serde_json` is not bit-exact on
/// round trip, which is a pre-existing property of that formatter, not of
/// this module — so the properties below are checked over floats it *is*
/// bit-exact for, rather than asserting a stronger guarantee than the
/// dependency provides.
fn round_trips_through_serde_json(f: f64) -> bool {
    serde_json::to_string(&f)
        .ok()
        .and_then(|text| serde_json::from_str::<f64>(&text).ok())
        .is_some_and(|back| back.to_bits() == f.to_bits())
}

/// Any finite JSON value: `serde_json::Value` cannot hold NaN/infinity
/// without `arbitrary_precision`, but builds one directly to be explicit
/// about what this module's input domain is.
fn arbitrary_json() -> impl Strategy<Value = JsonValue> {
    let leaf = prop_oneof![
        Just(JsonValue::Null),
        any::<bool>().prop_map(JsonValue::Bool),
        any::<i64>().prop_map(|n| json!(n)),
        any::<f64>()
            .prop_filter("finite and serde_json round-trip stable", |f| {
                f.is_finite() && round_trips_through_serde_json(*f)
            })
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

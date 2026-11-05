//! The canonical encoder: a key-sorting [`Serialize`] wrapper around
//! [`JsonValue`], so string escaping and number formatting are exactly
//! `serde_json`'s own (see the module docs in `super`) instead of a
//! hand-rolled copy of them.

use crate::json::JsonValue;
use serde::ser::{SerializeMap, SerializeSeq};
use serde::{Serialize, Serializer};

/// Encode `value` as Linch canonical JSON v1 (see the `canonical` module
/// docs for the exact rules).
///
/// Recurses without a depth limit; pass untrusted input through
/// [`super::check_depth`] first.
pub fn to_canonical_string(value: &JsonValue) -> String {
    serde_json::to_string(&Canonical(value)).expect("canonical JSON values always serialize")
}

/// Serializes as `value`, except objects are emitted with keys sorted by
/// UTF-8 byte order (Unicode code point order) instead of insertion order.
struct Canonical<'a>(&'a JsonValue);

impl Serialize for Canonical<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self.0 {
            JsonValue::Null | JsonValue::Bool(_) | JsonValue::Number(_) | JsonValue::String(_) => {
                self.0.serialize(serializer)
            }
            JsonValue::Array(items) => {
                let mut seq = serializer.serialize_seq(Some(items.len()))?;
                for item in items {
                    seq.serialize_element(&Canonical(item))?;
                }
                seq.end()
            }
            JsonValue::Object(map) => {
                let mut entries: Vec<(&String, &JsonValue)> = map.iter().collect();
                entries.sort_unstable_by_key(|(key, _)| *key);
                let mut ser_map = serializer.serialize_map(Some(entries.len()))?;
                for (key, value) in entries {
                    ser_map.serialize_entry(key, &Canonical(value))?;
                }
                ser_map.end()
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn keys_sort_by_code_point_regardless_of_insertion_order() {
        let value = json!({"b": 1, "a": [2, "é"], "B": 2});
        assert_eq!(to_canonical_string(&value), r#"{"B":2,"a":[2,"é"],"b":1}"#);
    }

    #[test]
    fn separators_have_no_whitespace() {
        assert_eq!(
            to_canonical_string(&json!({"x": true, "y": [1, 2]})),
            r#"{"x":true,"y":[1,2]}"#
        );
    }

    #[test]
    fn control_characters_use_short_or_u00xx_escapes() {
        let value = json!({"k": "\u{1}\u{1f}\u{7f}\n\t\r\u{8}\u{c}\"\\"});
        assert_eq!(
            to_canonical_string(&value),
            "{\"k\":\"\\u0001\\u001f\u{7f}\\n\\t\\r\\b\\f\\\"\\\\\"}"
        );
    }

    #[test]
    fn non_ascii_is_kept_as_utf8_not_escaped() {
        assert_eq!(to_canonical_string(&json!("é☃😀")), "\"é☃😀\"");
    }

    #[test]
    fn nested_containers_round_trip_through_serde_json() {
        let value = json!({"a": {"b": [1, {"c": null}]}});
        let text = to_canonical_string(&value);
        let reparsed: JsonValue = serde_json::from_str(&text).unwrap();
        assert_eq!(reparsed, value);
    }
}

//! The canonical encoder: a hand-rolled recursive writer, instead of handing
//! [`JsonValue`] to `serde_json`'s own [`Serialize`](serde::Serialize)
//! machinery, so it can enforce [`super::MAX_ENCODE_DEPTH`] as it goes and
//! format floats to match Python's `repr` (see the module docs in `super`).
//! String escaping still delegates to `serde_json`'s own escape table
//! rather than re-implementing one.

use std::io::Write as _;

use crate::json::JsonValue;

use super::float::format_f64;
use super::{CanonicalError, MAX_ENCODE_DEPTH};

/// Encode `value` as Linch canonical JSON v1 (see the `canonical` module
/// docs for the exact rules).
///
/// # Errors
///
/// Returns [`CanonicalError::DepthExceeded`] rather than recursing past
/// [`super::MAX_ENCODE_DEPTH`], so this is safe to call on untrusted input
/// directly. Callers with a stricter, business-chosen limit (for example
/// [`super::JOURNAL_MAX_DEPTH`]) should still run [`super::check_depth`]
/// first to get that lower limit enforced.
pub fn to_canonical_string(value: &JsonValue) -> Result<String, CanonicalError> {
    let mut out = Vec::new();
    encode(value, 0, &mut out)?;
    Ok(String::from_utf8(out).expect("canonical JSON is always valid UTF-8"))
}

/// Writes `value`'s canonical encoding into `out`, with keys sorted by
/// UTF-8 byte order (Unicode code point order) instead of insertion order.
///
/// `depth` is the nesting depth of `value` itself (the root is 0); only
/// objects and arrays are checked against [`MAX_ENCODE_DEPTH`], matching
/// [`super::check_depth`]'s convention, so recursion here never exceeds
/// `MAX_ENCODE_DEPTH + 1` stack frames.
fn encode(value: &JsonValue, depth: usize, out: &mut Vec<u8>) -> Result<(), CanonicalError> {
    match value {
        JsonValue::Null => out.extend_from_slice(b"null"),
        JsonValue::Bool(true) => out.extend_from_slice(b"true"),
        JsonValue::Bool(false) => out.extend_from_slice(b"false"),
        JsonValue::Number(n) => encode_number(n, out),
        JsonValue::String(s) => encode_string(s, out),
        JsonValue::Array(items) => {
            if depth > MAX_ENCODE_DEPTH {
                return Err(CanonicalError::DepthExceeded {
                    max_depth: MAX_ENCODE_DEPTH,
                });
            }
            out.push(b'[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                encode(item, depth + 1, out)?;
            }
            out.push(b']');
        }
        JsonValue::Object(map) => {
            if depth > MAX_ENCODE_DEPTH {
                return Err(CanonicalError::DepthExceeded {
                    max_depth: MAX_ENCODE_DEPTH,
                });
            }
            let mut entries: Vec<(&String, &JsonValue)> = map.iter().collect();
            entries.sort_unstable_by_key(|(key, _)| *key);
            out.push(b'{');
            for (i, (key, value)) in entries.into_iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                encode_string(key, out);
                out.push(b':');
                encode(value, depth + 1, out)?;
            }
            out.push(b'}');
        }
    }
    Ok(())
}

/// Appends `n` formatted exactly as Python's `json.dumps` would: an integer
/// prints as plain decimal digits (already what `serde_json`'s own
/// `Display` does, so this reuses it), a float uses [`format_f64`].
fn encode_number(n: &serde_json::Number, out: &mut Vec<u8>) {
    if n.is_f64() {
        let f = n.as_f64().expect("is_f64() guarantees as_f64() succeeds");
        out.extend_from_slice(format_f64(f).as_bytes());
    } else {
        write!(out, "{n}").expect("writing to a Vec<u8> never fails");
    }
}

/// Appends `s` quoted and escaped exactly as `serde_json` would, reusing
/// its escape table instead of re-implementing one.
fn encode_string(s: &str, out: &mut Vec<u8>) {
    serde_json::to_writer(out, s).expect("a &str always serializes to a JSON string");
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn keys_sort_by_code_point_regardless_of_insertion_order() {
        let value = json!({"b": 1, "a": [2, "é"], "B": 2});
        assert_eq!(
            to_canonical_string(&value).unwrap(),
            r#"{"B":2,"a":[2,"é"],"b":1}"#
        );
    }

    #[test]
    fn separators_have_no_whitespace() {
        assert_eq!(
            to_canonical_string(&json!({"x": true, "y": [1, 2]})).unwrap(),
            r#"{"x":true,"y":[1,2]}"#
        );
    }

    #[test]
    fn control_characters_use_short_or_u00xx_escapes() {
        let value = json!({"k": "\u{1}\u{1f}\u{7f}\n\t\r\u{8}\u{c}\"\\"});
        assert_eq!(
            to_canonical_string(&value).unwrap(),
            "{\"k\":\"\\u0001\\u001f\u{7f}\\n\\t\\r\\b\\f\\\"\\\\\"}"
        );
    }

    #[test]
    fn non_ascii_is_kept_as_utf8_not_escaped() {
        assert_eq!(to_canonical_string(&json!("é☃😀")).unwrap(), "\"é☃😀\"");
    }

    #[test]
    fn nested_containers_round_trip_through_serde_json() {
        let value = json!({"a": {"b": [1, {"c": null}]}});
        let text = to_canonical_string(&value).unwrap();
        let reparsed: JsonValue = serde_json::from_str(&text).unwrap();
        assert_eq!(reparsed, value);
    }

    #[test]
    fn a_container_exactly_at_the_ceiling_still_encodes() {
        // `check_depth`'s boundary test names the same convention: a
        // container sitting exactly at `MAX_ENCODE_DEPTH` is allowed.
        let mut value = json!(0);
        for _ in 0..MAX_ENCODE_DEPTH {
            value = JsonValue::Array(vec![value]);
        }
        assert!(to_canonical_string(&value).is_ok());
    }
}

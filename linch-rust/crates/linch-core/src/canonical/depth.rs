//! Bounding container nesting before it reaches an unbounded encoder.

use crate::json::JsonValue;

use super::CanonicalError;

/// Deepest container nesting the model-call journal accepts (the root is
/// depth 0). Mirrors `MAX_DEPTH` in `linch-python`'s `check_journal_json`,
/// which this crate's journal-record callers reuse as their limit.
pub const JOURNAL_MAX_DEPTH: usize = 64;

/// Deepest container nesting [`check_depth`] or the canonical encoder
/// (`to_canonical_string`, `digest`, `digest_with_domain`) will ever
/// recurse into, no matter what limit a caller asks for.
///
/// `serde_json`'s own parser already refuses JSON text nested 128 levels
/// deep (pinned by `canonical::tests::serde_json_parse_limit_is_127_deep_but_not_128`),
/// so any [`JsonValue`] built by parsing untrusted text is already shallower
/// than that. 256 doubles that limit, leaving headroom for the handful of
/// extra levels Linch's own wrapping adds on top of a parsed value (a tool
/// input nested in a content block, a block in a message, a message in an
/// envelope, ...), while staying far below any depth that could threaten a
/// real thread's native stack: a few hundred frames of a small,
/// non-allocating recursive function is negligible next to a multi-megabyte
/// stack, unlike the multi-million-deep adversarial inputs this ceiling
/// exists to reject.
pub const MAX_ENCODE_DEPTH: usize = 256;

/// Reject `value` if any object or array nests deeper than `max_depth`.
///
/// The root is depth 0; only containers (objects, arrays) count toward the
/// limit — scalars never fail regardless of how deep they sit. A container
/// is rejected, without descending into its children, as soon as its own
/// depth exceeds `max_depth`, so the recursion depth itself never exceeds
/// `max_depth + 1` even on adversarially deep input.
///
/// The effective limit is capped at [`MAX_ENCODE_DEPTH`]: a `max_depth`
/// above it is lowered to it before checking, so this function's own
/// recursion never exceeds `MAX_ENCODE_DEPTH + 1` stack frames no matter
/// what the caller passes, and the returned error reports the limit that
/// was actually enforced.
pub fn check_depth(value: &JsonValue, max_depth: usize) -> Result<(), CanonicalError> {
    visit(value, 0, max_depth.min(MAX_ENCODE_DEPTH))
}

fn visit(value: &JsonValue, depth: usize, max_depth: usize) -> Result<(), CanonicalError> {
    match value {
        JsonValue::Array(items) => {
            if depth > max_depth {
                return Err(CanonicalError::DepthExceeded { max_depth });
            }
            for child in items {
                visit(child, depth + 1, max_depth)?;
            }
            Ok(())
        }
        JsonValue::Object(map) => {
            if depth > max_depth {
                return Err(CanonicalError::DepthExceeded { max_depth });
            }
            for child in map.values() {
                visit(child, depth + 1, max_depth)?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use serde_json::json;

    /// The deepest `check_depth`-relative depth any container in `value`
    /// sits at, root at depth 0 — i.e. the smallest `max_depth` that still
    /// makes `check_depth(value, max_depth)` succeed. A value with no
    /// container at all (a bare scalar) is `0`: `check_depth` never fails on
    /// it regardless of the limit.
    fn depth_of(value: &JsonValue) -> usize {
        fn deepest<'a>(children: impl Iterator<Item = &'a JsonValue>, depth: usize) -> usize {
            children
                .filter_map(|child| max_container_depth(child, depth + 1))
                .max()
                .unwrap_or(depth)
                .max(depth)
        }
        fn max_container_depth(value: &JsonValue, depth: usize) -> Option<usize> {
            match value {
                JsonValue::Array(items) => Some(deepest(items.iter(), depth)),
                JsonValue::Object(map) => Some(deepest(map.values(), depth)),
                _ => None,
            }
        }
        max_container_depth(value, 0).unwrap_or(0)
    }

    /// `depth` arrays nested around a leaf scalar (`nested(0)` is the bare
    /// leaf itself); its deepest container sits at `check_depth`-relative
    /// depth `depth - 1` (for `depth >= 1`).
    fn nested(depth: usize) -> JsonValue {
        let mut value = json!("leaf");
        for _ in 0..depth {
            value = json!([value]);
        }
        value
    }

    #[test]
    fn scalars_never_fail_at_any_limit() {
        assert!(check_depth(&json!("x"), 0).is_ok());
        assert!(check_depth(&json!(1), 0).is_ok());
        assert!(check_depth(&json!(null), 0).is_ok());
    }

    #[test]
    fn boundary_is_exactly_at_max_depth() {
        // `nested(JOURNAL_MAX_DEPTH + 1)` has JOURNAL_MAX_DEPTH + 1 arrays,
        // whose innermost one sits at depth JOURNAL_MAX_DEPTH — the deepest
        // container the limit still allows.
        let at_limit = nested(JOURNAL_MAX_DEPTH + 1);
        assert_eq!(depth_of(&at_limit), JOURNAL_MAX_DEPTH);
        assert!(check_depth(&at_limit, JOURNAL_MAX_DEPTH).is_ok());

        let over_limit = nested(JOURNAL_MAX_DEPTH + 2);
        assert_eq!(depth_of(&over_limit), JOURNAL_MAX_DEPTH + 1);
        assert_eq!(
            check_depth(&over_limit, JOURNAL_MAX_DEPTH),
            Err(CanonicalError::DepthExceeded {
                max_depth: JOURNAL_MAX_DEPTH
            })
        );
    }

    #[test]
    fn shared_children_are_not_cycles() {
        let shared = json!([1]);
        let value = json!({"a": shared.clone(), "b": shared});
        assert!(check_depth(&value, 1).is_ok());
    }

    fn arbitrary_json() -> impl Strategy<Value = JsonValue> {
        let leaf = prop_oneof![
            Just(JsonValue::Null),
            any::<bool>().prop_map(JsonValue::Bool),
            any::<i32>().prop_map(|n| json!(n)),
            ".*".prop_map(JsonValue::String),
        ];
        leaf.prop_recursive(6, 64, 8, |inner| {
            prop_oneof![
                prop::collection::vec(inner.clone(), 0..4).prop_map(JsonValue::Array),
                prop::collection::btree_map("[a-c]", inner, 0..4).prop_map(|m| json!(m)),
            ]
        })
    }

    proptest! {
        #[test]
        fn matches_reference_depth(value in arbitrary_json(), limit in 0usize..8) {
            let actual_depth = depth_of(&value);
            prop_assert_eq!(check_depth(&value, limit).is_ok(), limit >= actual_depth);
        }
    }

    #[test]
    fn check_depth_never_recurses_past_the_ceiling_whatever_max_depth_is() {
        // Built iteratively, not via `json!` (which would recurse while
        // expanding the macro and could overflow just constructing this).
        let mut value = json!(0);
        for _ in 0..2_000_000 {
            value = JsonValue::Array(vec![value]);
        }

        // A caller-supplied limit far above any real ceiling must not make
        // `check_depth` itself recurse 2,000,000 levels deep.
        assert_eq!(
            check_depth(&value, 1 << 30),
            Err(CanonicalError::DepthExceeded {
                max_depth: super::MAX_ENCODE_DEPTH
            })
        );

        // Dropping a 2,000,000-deep `Value` recurses in `Vec<Value>`'s own
        // `Drop`, which would itself overflow the stack on teardown.
        std::mem::forget(value);
    }
}

//! Bounding container nesting before it reaches an unbounded encoder.

use crate::json::JsonValue;

use super::CanonicalError;

/// Deepest container nesting the model-call journal accepts (the root is
/// depth 0). Mirrors `MAX_DEPTH` in `linch-python`'s `check_journal_json`,
/// which this crate's journal-record callers reuse as their limit.
pub const JOURNAL_MAX_DEPTH: usize = 64;

/// Reject `value` if any object or array nests deeper than `max_depth`.
///
/// The root is depth 0; only containers (objects, arrays) count toward the
/// limit — scalars never fail regardless of how deep they sit. A container
/// is rejected, without descending into its children, as soon as its own
/// depth exceeds `max_depth`, so recursion never goes deeper than
/// `max_depth + 1` frames even on adversarially deep input.
pub fn check_depth(value: &JsonValue, max_depth: usize) -> Result<(), CanonicalError> {
    visit(value, 0, max_depth)
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
}

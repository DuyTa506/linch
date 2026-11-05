//! JSON aliases shared by every wire type.
//!
//! Objects keep insertion order (`serde_json` is built with `preserve_order`),
//! so a value decoded and re-encoded keeps its key order. Canonical,
//! order-independent encoding lives in [`crate::canonical`].

/// Any JSON value.
pub use serde_json::Value as JsonValue;

/// A JSON object with insertion order preserved.
pub type JsonObject = serde_json::Map<String, JsonValue>;

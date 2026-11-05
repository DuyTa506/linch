//! Linch canonical JSON v1: a deterministic encoding for digests, signatures,
//! and content-addressed keys.
//!
//! It is **not** RFC 8785 (JCS) — it is exactly what
//! `json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
//! separators=(",", ":"))` produces in the Python reference implementation
//! (`linch._json.canonical_dumps`), re-specified here in terms this crate can
//! guarantee without a Python runtime:
//!
//! - **Object keys** are sorted by byte value of their UTF-8 encoding, which
//!   is Unicode code point order for any valid `str` key (this crate's keys
//!   are always valid UTF-8, so there is no separate case to handle).
//! - **No insignificant whitespace**: `,` and `:` separate items, nothing
//!   else.
//! - **Strings** escape only `"`, `\`, and the C0 control characters
//!   (`U+0000..=U+001F`) — the five short forms `\b \f \n \r \t` plus
//!   `\u00xx` (lowercase hex) for the rest. Everything else, including every
//!   non-ASCII character, is written out as its own UTF-8 bytes. This is what
//!   `serde_json`'s default (compact, non-`ensure_ascii`) string encoder
//!   already does, so [`to_canonical_string`] delegates to it rather than
//!   re-implementing an escape table.
//! - **Numbers** are serialized exactly as `serde_json` prints the
//!   [`JsonValue`](crate::JsonValue) number it was given — this crate makes
//!   no promise beyond what `serde_json` itself promises for a float. Common
//!   float literals happen to match Python's `repr`-based formatting
//!   (`1e16` prints as `1e+16` under both), but this is not guaranteed for
//!   every float, and at extreme magnitudes (observed around `1e269`) this
//!   `serde_json` version is not even bit-exact on its own format-then-parse
//!   round trip. Integers and the common JSON shapes (objects, arrays,
//!   strings, bools, null) match the Python reference byte-for-byte.
//!
//! [`check_depth`] bounds container nesting before encoding; it is a
//! separate, explicit step — [`to_canonical_string`] itself recurses without
//! a limit, so callers that accept untrusted input must call it first.

mod depth;
mod digest;
mod encode;

#[cfg(test)]
mod tests;

pub use depth::{JOURNAL_MAX_DEPTH, check_depth};
pub use digest::{Digest, digest, digest_with_domain};
pub use encode::to_canonical_string;

use thiserror::Error;

/// Errors from canonical JSON encoding, digesting, and depth checks.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum CanonicalError {
    /// A [`Digest`] string was not exactly 64 lowercase hex characters.
    #[error("digest must be exactly 64 lowercase hex characters")]
    InvalidDigest,
    /// A container (object or array) nested deeper than the caller's limit.
    #[error("value nests deeper than the maximum depth of {max_depth}")]
    DepthExceeded {
        /// The limit that was exceeded.
        max_depth: usize,
    },
}

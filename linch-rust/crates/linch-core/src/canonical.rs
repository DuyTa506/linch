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
//! - **Integers** inside `i64`/`u64` print as plain decimal digits, which
//!   `serde_json` already does exactly as Python would. An integer outside
//!   that range is a **documented limit**: it round-trips through `f64`
//!   (lossily, once it exceeds `f64`'s 53-bit mantissa) rather than keeping
//!   full precision, because this crate does not enable `serde_json`'s
//!   `arbitrary_precision` feature. That feature changes how `Number`
//!   deserializes in a way that breaks the internally tagged enums
//!   (`#[serde(tag = "type")]`) used throughout this crate, so it is off by
//!   choice, not oversight.
//! - **Finite floats** are formatted to match Python's `repr` byte for
//!   byte — shortest round-trip digits, fixed notation when
//!   `1e-4 <= |x| < 1e16`, otherwise scientific notation with a signed,
//!   at-least-two-digit exponent (`1e16` prints as `1e+16` under both,
//!   `1e-5` as `1e-05`) — which is *not* what `serde_json`'s own float
//!   formatter produces (see `canonical::float`'s module docs). The crate
//!   also enables `serde_json`'s `float_roundtrip` feature when *parsing* a
//!   canonical string back, so every finite float survives the round trip
//!   bit-identically; the default parser is only best-effort and is off by
//!   one ULP near `1e269`.
//!
//! [`to_canonical_string`] (and therefore [`digest`] and
//! [`digest_with_domain`]) never recurses past [`MAX_ENCODE_DEPTH`] — it
//! enforces that ceiling itself, so it is safe to call on untrusted input
//! directly. [`check_depth`] is a separate, *stricter* pre-check for
//! callers with a smaller business limit, such as [`JOURNAL_MAX_DEPTH`].

mod depth;
mod digest;
mod encode;
mod float;

#[cfg(test)]
mod tests;

pub use depth::{JOURNAL_MAX_DEPTH, MAX_ENCODE_DEPTH, check_depth};
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

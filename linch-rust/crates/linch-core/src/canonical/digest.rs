//! SHA-256 digests of canonical JSON, with an optional domain separator.

use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};

use crate::json::JsonValue;

use super::{CanonicalError, to_canonical_string};

/// A SHA-256 digest, displayed and serialized as 64 lowercase hex
/// characters. [`std::str::FromStr`] (and therefore [`Deserialize`]) accepts
/// only that exact form — wrong length, uppercase, or a non-hex character
/// are all rejected.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct Digest([u8; 32]);

impl Digest {
    /// Wrap raw SHA-256 output.
    pub(crate) fn from_bytes(bytes: [u8; 32]) -> Self {
        Self(bytes)
    }

    /// The raw 32-byte SHA-256 output.
    pub fn as_bytes(&self) -> &[u8; 32] {
        &self.0
    }
}

impl std::fmt::Display for Digest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        for byte in self.0 {
            write!(f, "{byte:02x}")?;
        }
        Ok(())
    }
}

impl std::str::FromStr for Digest {
    type Err = CanonicalError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let hex = text.as_bytes();
        if hex.len() != 64
            || !hex
                .iter()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
        {
            return Err(CanonicalError::InvalidDigest);
        }
        let mut bytes = [0u8; 32];
        let (pairs, _) = hex.as_chunks::<2>();
        for (out, pair) in bytes.iter_mut().zip(pairs) {
            *out = (hex_nibble(pair[0]) << 4) | hex_nibble(pair[1]);
        }
        Ok(Digest(bytes))
    }
}

/// Converts one already-validated lowercase hex digit.
fn hex_nibble(byte: u8) -> u8 {
    match byte {
        b'0'..=b'9' => byte - b'0',
        _ => byte - b'a' + 10,
    }
}

impl TryFrom<String> for Digest {
    type Error = CanonicalError;

    fn try_from(value: String) -> Result<Self, Self::Error> {
        value.parse()
    }
}

impl From<Digest> for String {
    fn from(value: Digest) -> Self {
        value.to_string()
    }
}

/// The SHA-256 digest of `value`'s canonical JSON encoding.
///
/// # Errors
///
/// Returns [`CanonicalError::DepthExceeded`] if `value` nests deeper than
/// [`super::MAX_ENCODE_DEPTH`] (see [`to_canonical_string`]).
pub fn digest(value: &JsonValue) -> Result<Digest, CanonicalError> {
    Ok(hash(&[to_canonical_string(value)?.as_bytes()]))
}

/// The SHA-256 digest of `domain`, a NUL separator, then `value`'s canonical
/// JSON encoding.
///
/// Mirrors the `domain\x00rest` separator `linch`'s workflow journal
/// (`src/linch/ext/workflow/journal.py`) uses to keep its key namespaces
/// (subagent calls, steps, interrupts) from colliding even when their
/// payloads happen to match. A NUL byte *inside* `domain` cannot create such
/// a collision: canonical JSON escapes every `U+0000` it encodes as the
/// six-byte sequence `\u0000`, so `value`'s canonical encoding never
/// contributes a raw `0x00` byte to the hashed bytes. The *last* raw NUL in
/// `domain || 0x00 || canonical(value)` is therefore always the separator
/// inserted here, never one embedded in `domain`, so the two parts can
/// always be told apart.
///
/// # Errors
///
/// Returns [`CanonicalError::DepthExceeded`] if `value` nests deeper than
/// [`super::MAX_ENCODE_DEPTH`] (see [`to_canonical_string`]).
pub fn digest_with_domain(domain: &str, value: &JsonValue) -> Result<Digest, CanonicalError> {
    Ok(hash(&[
        domain.as_bytes(),
        b"\0",
        to_canonical_string(value)?.as_bytes(),
    ]))
}

fn hash(parts: &[&[u8]]) -> Digest {
    let mut hasher = Sha256::new();
    for part in parts {
        hasher.update(part);
    }
    Digest(hasher.finalize().into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::str::FromStr;

    #[test]
    fn digest_displays_as_lowercase_hex_and_round_trips() {
        let d = digest(&json!({"a": 1})).unwrap();
        let text = d.to_string();
        assert_eq!(text.len(), 64);
        assert!(
            text.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        );
        assert_eq!(Digest::from_str(&text).unwrap(), d);
        assert_eq!(serde_json::to_string(&d).unwrap(), format!("\"{text}\""));
        assert_eq!(
            serde_json::from_str::<Digest>(&format!("\"{text}\"")).unwrap(),
            d
        );
    }

    #[test]
    fn from_str_rejects_anything_but_64_lowercase_hex_chars() {
        let valid = "a".repeat(64);
        assert!(Digest::from_str(&valid).is_ok());
        assert_eq!(
            Digest::from_str(&"a".repeat(63)),
            Err(CanonicalError::InvalidDigest)
        );
        assert_eq!(
            Digest::from_str(&"a".repeat(65)),
            Err(CanonicalError::InvalidDigest)
        );
        assert_eq!(
            Digest::from_str(&"A".repeat(64)),
            Err(CanonicalError::InvalidDigest)
        );
        assert_eq!(
            Digest::from_str(&format!("sha256:{}", "a".repeat(64))),
            Err(CanonicalError::InvalidDigest)
        );
        assert_eq!(
            Digest::from_str(&"g".repeat(64)),
            Err(CanonicalError::InvalidDigest)
        );
    }

    #[test]
    fn deserialize_rejects_anything_but_64_lowercase_hex_chars() {
        assert!(serde_json::from_str::<Digest>(&format!("\"{}\"", "A".repeat(64))).is_err());
        assert!(serde_json::from_str::<Digest>(&format!("\"{}\"", "a".repeat(63))).is_err());
        assert!(serde_json::from_str::<Digest>("123").is_err());
    }

    #[test]
    fn domain_separation_changes_the_digest() {
        let value = json!({"name": "x"});
        assert_ne!(
            digest(&value).unwrap(),
            digest_with_domain("step", &value).unwrap()
        );
        assert_ne!(
            digest_with_domain("step", &value).unwrap(),
            digest_with_domain("agent", &value).unwrap()
        );
    }

    #[test]
    fn canonical_json_never_contains_a_raw_nul_byte() {
        // The premise `digest_with_domain`'s doc rests on: a NUL inside a
        // JSON string is always escaped as `\u0000`, never written raw.
        let encoded = to_canonical_string(&json!("a\u{0}b")).unwrap();
        assert!(!encoded.as_bytes().contains(&0));
        assert_eq!(encoded, "\"a\\u0000b\"");
    }

    #[test]
    fn nul_inside_domain_cannot_be_confused_with_the_separator() {
        // If the separator could be confused with a NUL embedded in
        // `domain`, splitting the same byte sequence differently between
        // `domain` and `value` could collide. It cannot: canonical JSON
        // never emits a raw NUL, so the last NUL in the hashed bytes is
        // always the real separator.
        let a = digest_with_domain("x\u{0}y", &json!("z")).unwrap();
        let b = digest_with_domain("x", &json!("y\u{0}z")).unwrap();
        assert_ne!(a, b);
    }

    #[test]
    fn pinned_vectors_match_an_independent_sha256_oracle() {
        // Computed with Python's hashlib against the same canonical bytes,
        // independently of this crate's own implementation.
        assert_eq!(
            digest(&json!({})).unwrap().to_string(),
            "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        );
        assert_eq!(
            digest(&json!([])).unwrap().to_string(),
            "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
        );
        assert_eq!(
            digest(&json!(null)).unwrap().to_string(),
            "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
        );
        assert_eq!(
            digest(&json!({"b": 1, "a": [2, "é"]})).unwrap().to_string(),
            "2db0b9da091c3e5533b64b892d5ff09a608ab67e3a978160d9fe6fd2aef5f167"
        );
        assert_eq!(
            digest(&json!({"x": true, "y": false})).unwrap().to_string(),
            "be43c656e6ca2d6beccfda71b2d9a696b2b434c77f43f74bd451c441713568ed"
        );
        // Exercises the escape table end-to-end: the five short forms, a
        // lowercase \u00xx control escape, and DEL (0x7f) left raw.
        assert_eq!(
            digest(&json!({"k": "\u{1}\u{1f}\u{7f}\n\t\r\u{8}\u{c}\"\\"}))
                .unwrap()
                .to_string(),
            "5e9458bd35f2eb25eeb85f87ed3f1392925f953efb93e0bb1faae214d6c9f019"
        );
        // Pins sign-of-zero, which `JsonValue`'s `PartialEq` would hide.
        assert_eq!(
            digest(&json!(-0.0)).unwrap().to_string(),
            "c26617c7ccbcaa6631b45d851b8cf56e21d2ca624bdb1193afdbd4b560702cec"
        );
        assert_eq!(
            digest_with_domain("step", &json!({"name": "x"}))
                .unwrap()
                .to_string(),
            "fd800022434ad5111c8aa595d91ee01aaf3c3bba94d7e3d8aed749b3d51dd194"
        );
    }
}

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
pub fn digest(value: &JsonValue) -> Digest {
    hash(&[to_canonical_string(value).as_bytes()])
}

/// The SHA-256 digest of `domain`, a NUL separator, then `value`'s canonical
/// JSON encoding.
///
/// Mirrors the `domain\x00rest` separator `linch`'s workflow journal
/// (`src/linch/ext/workflow/journal.py`) uses to keep its key namespaces
/// (subagent calls, steps, interrupts) from colliding even when their
/// payloads happen to match. `domain` must not itself contain a NUL byte, or
/// two domains could be made to collide.
pub fn digest_with_domain(domain: &str, value: &JsonValue) -> Digest {
    hash(&[
        domain.as_bytes(),
        b"\0",
        to_canonical_string(value).as_bytes(),
    ])
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
        let d = digest(&json!({"a": 1}));
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
    fn domain_separation_changes_the_digest() {
        let value = json!({"name": "x"});
        assert_ne!(digest(&value), digest_with_domain("step", &value));
        assert_ne!(
            digest_with_domain("step", &value),
            digest_with_domain("agent", &value)
        );
    }

    #[test]
    fn pinned_vectors_match_an_independent_sha256_oracle() {
        // Computed with Python's hashlib against the same canonical bytes,
        // independently of this crate (see .migration/p2/W4.md).
        assert_eq!(
            digest(&json!({})).to_string(),
            "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        );
        assert_eq!(
            digest(&json!([])).to_string(),
            "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
        );
        assert_eq!(
            digest(&json!(null)).to_string(),
            "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
        );
        assert_eq!(
            digest(&json!({"b": 1, "a": [2, "é"]})).to_string(),
            "2db0b9da091c3e5533b64b892d5ff09a608ab67e3a978160d9fe6fd2aef5f167"
        );
        assert_eq!(
            digest(&json!({"x": true, "y": false})).to_string(),
            "be43c656e6ca2d6beccfda71b2d9a696b2b434c77f43f74bd451c441713568ed"
        );
        assert_eq!(
            digest_with_domain("step", &json!({"name": "x"})).to_string(),
            "fd800022434ad5111c8aa595d91ee01aaf3c3bba94d7e3d8aed749b3d51dd194"
        );
    }
}

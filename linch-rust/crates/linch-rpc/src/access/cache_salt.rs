//! The tenant's prompt-cache salt: HMAC-SHA256 of the tenant's name under a
//! secret only the operator holds, so the salt is stable for one tenant
//! across restarts and processes, differs between tenants, and does not
//! name the tenant on the wire or let anyone who sees it compute another's.

use hmac::{Hmac, KeyInit, Mac};
use linch::providers::CacheSalt;
use sha2::Sha256;

/// Keeps this derivation apart from any other use of the same secret.
const DOMAIN: &str = "linch.cache_salt:";

/// The least bytes an operator's secret may have: a shorter one could be
/// guessed, and with it every tenant's salt.
pub const MIN_SECRET_BYTES: usize = 16;

/// The salt of tenant `tenant` under `secret`: 64 lower-case hex characters
/// (256 bits, the size vLLM recommends).
pub(super) fn derive(secret: &[u8], tenant: &str) -> Option<CacheSalt> {
    let mut message = String::from(DOMAIN);
    message.push_str(tenant);
    let mac = hmac_sha256(secret, message.as_bytes())?;
    CacheSalt::new(
        mac.iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
}

/// RFC 2104 HMAC over SHA-256, from the `hmac` crate the Postgres driver
/// already pulls in.
fn hmac_sha256(key: &[u8], message: &[u8]) -> Option<[u8; 32]> {
    // HMAC takes a key of any length, so this is `Some`; never a constant
    // fallback, which would give every tenant one salt.
    let mut mac = Hmac::<Sha256>::new_from_slice(key).ok()?;
    mac.update(message);
    Some(mac.finalize().into_bytes().into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(bytes: [u8; 32]) -> String {
        bytes.iter().map(|byte| format!("{byte:02x}")).collect()
    }

    /// RFC 4231 test cases 1, 2 and 6 (a key longer than the block).
    #[test]
    fn hmac_matches_the_rfc_4231_vectors() {
        let cases: [(&[u8], &[u8], &str); 3] = [
            (
                &[0x0b; 20],
                b"Hi There",
                "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7",
            ),
            (
                b"Jefe",
                b"what do ya want for nothing?",
                "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843",
            ),
            (
                &[0xaa; 131],
                b"Test Using Larger Than Block-Size Key - Hash Key First",
                "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54",
            ),
        ];
        for (key, data, expected) in cases {
            assert_eq!(hex(hmac_sha256(key, data).unwrap()), expected);
        }
    }

    #[test]
    fn a_salt_is_stable_per_tenant_and_differs_between_tenants_and_secrets() {
        let secret = b"0123456789abcdef-operator-secret";
        let acme = derive(secret, "acme").unwrap();
        assert_eq!(acme, derive(secret, "acme").unwrap());
        assert_ne!(acme, derive(secret, "beta").unwrap());
        assert_ne!(
            acme,
            derive(b"another-operator-secret-0000", "acme").unwrap()
        );
        assert_eq!(acme.expose().len(), 64);
        assert!(acme.expose().bytes().all(|b| b.is_ascii_hexdigit()));
        assert!(!acme.expose().contains("acme"));
    }
}

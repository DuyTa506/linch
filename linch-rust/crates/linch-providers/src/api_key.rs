//! A provider credential that never prints itself, plus the scrubber that
//! keeps it out of error text.

use std::fmt;
use std::sync::Arc;

const REDACTED: &str = "[redacted]";

/// A provider API key.
///
/// `Debug` prints `ApiKey([redacted])` and there is no `Display`, so the key
/// cannot leak through `{:?}` on an options struct or an error.
#[derive(Clone)]
pub struct ApiKey(Arc<str>);

impl ApiKey {
    /// Wrap a key. Returns `None` for an empty or all-whitespace key.
    pub fn new(key: impl Into<String>) -> Option<Self> {
        let key = key.into();
        let trimmed = key.trim();
        (!trimmed.is_empty()).then(|| Self(trimmed.into()))
    }

    /// Read a key from the process environment variable `name`.
    pub fn from_env(name: &str) -> Option<Self> {
        std::env::var(name).ok().and_then(Self::new)
    }

    pub(crate) fn expose(&self) -> &str {
        &self.0
    }

    /// Remove this key and every `Bearer <token>` from `text`.
    ///
    /// Error bodies and transport messages may echo request headers; every
    /// string that ends up in a `ProviderError` passes through here first.
    pub(crate) fn redact(&self, text: &str) -> String {
        redact_bearer(&text.replace(self.expose(), REDACTED))
    }
}

/// Remove `key`, when there is one, and every `Bearer <token>` from `text`.
///
/// Servers that run without a key still get bearer tokens scrubbed, since a
/// proxy in front of them may add one.
pub(crate) fn scrub(key: Option<&ApiKey>, text: &str) -> String {
    match key {
        Some(key) => key.redact(text),
        None => redact_bearer(text),
    }
}

impl fmt::Debug for ApiKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "ApiKey({REDACTED})")
    }
}

/// Replace the token after each case-insensitive `bearer ` with `[redacted]`.
fn redact_bearer(text: &str) -> String {
    const PREFIX: &str = "bearer ";
    // RFC 6750 token68 characters.
    let is_token = |c: char| c.is_ascii_alphanumeric() || "-._~+/=".contains(c);
    let lower = text.to_ascii_lowercase();
    let mut out = String::with_capacity(text.len());
    let mut rest = 0;
    while let Some(found) = lower[rest..].find(PREFIX) {
        let token_start = rest + found + PREFIX.len();
        let token_len = text[token_start..]
            .find(|c: char| !is_token(c))
            .unwrap_or(text.len() - token_start);
        out.push_str(&text[rest..token_start]);
        if token_len > 0 {
            out.push_str(REDACTED);
        }
        rest = token_start + token_len;
    }
    out.push_str(&text[rest..]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debug_never_shows_the_key() {
        let key = ApiKey::new("sk-secret-123").unwrap();
        assert_eq!(format!("{key:?}"), "ApiKey([redacted])");
        assert!(ApiKey::new("  ").is_none());
    }

    #[test]
    fn redact_removes_the_key_and_any_bearer_token() {
        let key = ApiKey::new("sk-secret-123").unwrap();
        let text = r#"{"headers":{"Authorization":"Bearer sk-secret-123","x":"BEARER other-token"}} raw sk-secret-123"#;
        let clean = key.redact(text);
        assert!(!clean.contains("sk-secret-123"), "{clean}");
        assert!(!clean.contains("other-token"), "{clean}");
        assert_eq!(
            clean,
            r#"{"headers":{"Authorization":"Bearer [redacted]","x":"BEARER [redacted]"}} raw [redacted]"#
        );
    }

    #[test]
    fn scrub_without_a_key_still_removes_bearer_tokens() {
        assert_eq!(
            scrub(None, "Authorization: Bearer proxy-token"),
            "Authorization: Bearer [redacted]"
        );
        let key = ApiKey::new("sk-secret-123").unwrap();
        assert_eq!(scrub(Some(&key), "raw sk-secret-123"), "raw [redacted]");
    }

    #[test]
    fn redact_leaves_plain_text_alone() {
        let key = ApiKey::new("sk-secret-123").unwrap();
        assert_eq!(key.redact("rate limit reached"), "rate limit reached");
        assert_eq!(key.redact("trailing bearer "), "trailing bearer ");
    }
}

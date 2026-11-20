//! Where an offloaded tool result is written, and how a pointer to it is
//! read back out of a result text.
//!
//! The offload hook (`linch_tools::vfs::ResultOffload`) names the file and
//! writes the pointer; the loop guard strips the name from the pointer so
//! that two identical results never look different. Both go through this
//! module, so they cannot disagree on the shape.

use std::borrow::Cow;
use std::fmt::Write as _;

use sha2::{Digest as _, Sha256};

/// The words before the quoted path in a pointer, as the model reads them:
/// `... offloaded to '/offload/s1/big_call_1_3fa9c1d2e4b5a678.txt' ...`.
pub const OFFLOADED_TO: &str = "offloaded to '";

/// What [`without_offload_path`] puts where the path was.
const STRIPPED: &str = "<offloaded>";

/// The most bytes of an id kept readable in a path segment.
const SEGMENT_KEPT: usize = 32;

/// Hex digits of a digest in a path.
const DIGEST_HEX: usize = 16;

/// `{prefix}/{session}/{tool}_{call id}_{digest}.txt`, the file of one
/// offloaded result.
///
/// The digest is of the stored text, so the name is made without asking the
/// backend what exists: the same result of the same call is the same file (a
/// retry, a loop), and a different result of a call id that a provider
/// repeats is a different file. Two writers of one name therefore write the
/// same text. Each id is made one short path segment by `segment`.
///
/// Args:
///     prefix: Normalized directory the results go under.
///     session: Session that owns the call.
///     tool: Name of the tool that ran.
///     call_id: Provider call id of the tool use.
///     text: The full text that is stored.
///
/// Returns:
///     A normalized path with the prefix's segments, then the session's
///     directory, then the file.
pub fn offload_path(prefix: &str, session: &str, tool: &str, call_id: &str, text: &str) -> String {
    let digest = hex(&Sha256::digest(text.as_bytes()), DIGEST_HEX);
    let prefix = prefix.trim_end_matches('/');
    let (session, tool, call_id) = (segment(session), segment(tool), segment(call_id));
    format!("{prefix}/{session}/{tool}_{call_id}_{digest}.txt")
}

/// `text` with the path of every pointer in it (see [`OFFLOADED_TO`])
/// replaced by a fixed word, so results that differ only in where they
/// were offloaded compare equal.
pub fn without_offload_path(text: &str) -> Cow<'_, str> {
    let paths: Vec<&str> = text
        .split(OFFLOADED_TO)
        .skip(1)
        .filter_map(|rest| rest.split_once('\'').map(|(path, _)| path))
        .filter(|path| !path.is_empty())
        .collect();
    let mut out = Cow::Borrowed(text);
    for path in paths {
        out = Cow::Owned(out.replace(path, STRIPPED));
    }
    out
}

/// `id` as one path segment: an id of at most 48 bytes from `[A-Za-z0-9_.-]`
/// that is not all dots is kept as it is; any other id (empty, long, with a
/// `/`, a dot-only name, a non-ASCII byte) becomes its first readable bytes,
/// a `~` and 16 hex digits of its SHA-256. `~` is not in the plain set, so
/// the two forms never meet, and the digest keeps distinct ids apart.
fn segment(id: &str) -> String {
    let plain = |byte: u8| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.');
    let kept = id.len() <= 48 && id.bytes().all(plain) && !id.bytes().all(|byte| byte == b'.');
    if kept {
        return id.to_owned();
    }
    let readable: String = id
        .bytes()
        .filter(|byte| plain(*byte) && *byte != b'.')
        .take(SEGMENT_KEPT)
        .map(char::from)
        .collect();
    let digest = hex(&Sha256::digest(id.as_bytes()), DIGEST_HEX);
    format!("{readable}~{digest}")
}

/// The first `digits` hex digits of `bytes`.
fn hex(bytes: &[u8], digits: usize) -> String {
    let mut out = String::with_capacity(digits);
    for byte in bytes.iter().take(digits.div_ceil(2)) {
        let _ = write!(out, "{byte:02x}");
    }
    out.truncate(digits);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_plain_id_is_kept_and_any_other_is_one_bounded_segment() {
        assert_eq!(segment("call_0"), "call_0");
        assert_eq!(segment("toolu_01A-b.c"), "toolu_01A-b.c");
        for id in [
            "",
            ".",
            "..",
            "a/b",
            "../x",
            "é",
            "a b",
            "a~b",
            &"x".repeat(49),
        ] {
            let made = segment(id);
            assert!(made.contains('~'), "{id:?} -> {made}");
            assert!(!made.contains('/') && made != "." && made != "..", "{made}");
            assert!(made.len() <= SEGMENT_KEPT + 1 + DIGEST_HEX, "{made}");
        }
        assert_ne!(segment("a/b"), segment("a_b"));
        assert_ne!(segment("é"), segment("ê"));
        assert_ne!(segment(&"x".repeat(60)), segment(&"x".repeat(61)));
        assert_eq!(segment("a/b"), segment("a/b"));
    }

    #[test]
    fn the_path_is_deterministic_and_follows_the_text() {
        let path = |text: &str| offload_path("/offload", "s1", "big", "call_1", text);
        assert_eq!(path("x"), path("x"));
        assert_ne!(path("x"), path("y"));
        let made = path("x");
        assert!(made.starts_with("/offload/s1/big_call_1_"), "{made}");
        assert!(made.ends_with(".txt"));
        assert_eq!(made.split('/').count(), 4);
    }

    #[test]
    fn a_pointer_loses_its_path_wherever_the_path_is_named() {
        let pointer = |path: &str| {
            format!(
                "head\n\n[Tool result truncated: offloaded to '{path}'. Use \
                 read_file(path=\"{path}\", offset=...) or ls(\"/offload/s1\").]"
            )
        };
        let (one, two) = (
            pointer("/offload/s1/a_1_00.txt"),
            pointer("/offload/s1/a_2_ff.txt"),
        );
        assert_ne!(one, two);
        assert_eq!(without_offload_path(&one), without_offload_path(&two));
        assert!(!without_offload_path(&one).contains("a_1_00"));
        let plain = "no pointer here";
        assert!(matches!(without_offload_path(plain), Cow::Borrowed(_)));
    }
}

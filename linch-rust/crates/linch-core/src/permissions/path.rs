//! Target paths for path rules: the field table, lexical normalization, and
//! pattern matching. Nothing here touches the filesystem, so symlinks are
//! not followed; the tool that opens a file confines what it really opens.

use super::glob::segments_match;
use super::{PathField, PermissionPolicy};

/// Where a file tool keeps its path, borrowed from the policy or the
/// built-in table.
#[derive(Debug, Clone, Copy)]
pub(super) struct FieldSpec<'a> {
    pub field: &'a str,
    pub default_to_cwd: bool,
    pub virtual_root: bool,
}

impl PathField {
    pub(super) fn spec(&self) -> FieldSpec<'_> {
        FieldSpec {
            field: &self.field,
            default_to_cwd: self.default_to_cwd,
            virtual_root: self.virtual_root,
        }
    }
}

/// The path fields of Linch's built-in file tools.
fn builtin_field(tool: &str) -> Option<FieldSpec<'static>> {
    let (field, default_to_cwd, virtual_root) = match tool {
        "Read" | "Write" | "Edit" => ("file_path", false, false),
        "Glob" | "Grep" => ("path", true, false),
        "read_file" | "write_file" | "edit_file" => ("path", false, true),
        "ls" => ("prefix", true, true),
        _ => return None,
    };
    Some(FieldSpec {
        field,
        default_to_cwd,
        virtual_root,
    })
}

/// The path field of `tool`: the policy's table first, then the built-ins.
pub(super) fn field<'a>(policy: &'a PermissionPolicy, tool: &str) -> Option<FieldSpec<'a>> {
    match policy.path_fields.get(tool) {
        Some(field) => Some(field.spec()),
        None => builtin_field(tool),
    }
}

/// Appends the segments of `path` to `out`, resolving `.`, `..` and
/// repeated `/` lexically. `..` at the filesystem root stays there.
pub(super) fn push_segments<'a>(out: &mut Vec<&'a str>, path: &'a str) {
    for segment in path.split('/') {
        match segment {
            "" | "." => {}
            ".." => {
                out.pop();
            }
            _ => out.push(segment),
        }
    }
}

/// Absolute, normalized segments of `raw` for a call whose working
/// directory is `cwd` (relative to `root` when relative, `root` when
/// absent). An absolute `raw` ignores both.
pub(super) fn resolve<'a>(root: &'a str, cwd: Option<&'a str>, raw: &'a str) -> Vec<&'a str> {
    let mut out = Vec::new();
    if !raw.starts_with('/') {
        if !cwd.is_some_and(|dir| dir.starts_with('/')) {
            push_segments(&mut out, root);
        }
        if let Some(dir) = cwd {
            push_segments(&mut out, dir);
        }
    }
    push_segments(&mut out, raw);
    out
}

/// Whether one [`super::PathRule::paths`] entry matches `target`. Absolute
/// patterns match the absolute target; relative ones match the part below
/// `root`, so a target outside the root never matches them (and so does
/// match their `!` inversion).
pub(super) fn pattern_matches(pattern: &str, target: &[&str], root: &[&str]) -> bool {
    let (inverted, body) = match pattern.strip_prefix('!') {
        Some(body) => (true, body),
        None => (false, pattern),
    };
    let segments: Vec<&str> = body
        .split('/')
        .filter(|s| !s.is_empty() && *s != ".")
        .collect();
    let matched = if body.starts_with('/') {
        segments_match(&segments, target)
    } else {
        target
            .strip_prefix(root)
            .is_some_and(|inside| segments_match(&segments, inside))
    };
    matched != inverted
}

/// Whether a path pattern can mean something: a non-blank body without a
/// `..` segment (patterns are normalized text, not paths to walk).
pub(super) fn valid_pattern(pattern: &str) -> bool {
    let body = pattern.strip_prefix('!').unwrap_or(pattern);
    !body.trim().is_empty() && !body.split('/').any(|s| s == "..")
}

#[cfg(test)]
mod tests {
    use std::path::{Component, Path};

    use proptest::prelude::*;

    use super::*;

    #[test]
    fn resolve_is_lexical() {
        assert_eq!(
            resolve("/p", None, "src/../secrets/t"),
            ["p", "secrets", "t"]
        );
        assert_eq!(resolve("/p", Some("sub"), "./a//b"), ["p", "sub", "a", "b"]);
        assert_eq!(resolve("/p", Some("/tmp"), "a"), ["tmp", "a"]);
        assert_eq!(resolve("/p", Some("sub"), "/etc/x"), ["etc", "x"]);
        assert_eq!(resolve("/p", None, "../../../../etc"), ["etc"]);
        assert!(resolve("/p", None, "..").is_empty());
    }

    #[test]
    fn relative_patterns_stay_below_the_root() {
        let root = ["p"];
        assert!(pattern_matches("secrets/**", &["p", "secrets", "k"], &root));
        assert!(!pattern_matches("secrets/**", &["secrets", "k"], &root));
        assert!(pattern_matches("!secrets/**", &["etc", "passwd"], &root));
        assert!(pattern_matches("/etc/**", &["etc", "passwd"], &root));
        assert!(pattern_matches("./src/*.rs", &["p", "src", "a.rs"], &root));
        assert!(!valid_pattern("../x"));
        assert!(!valid_pattern("!"));
        assert!(!valid_pattern(" "));
        assert!(valid_pattern("/etc/**"));
    }

    /// Independent reference: `std::path` components, popping on `..`.
    fn reference(path: &str) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        for component in Path::new(path).components() {
            match component {
                Component::Normal(s) => out.push(s.to_string_lossy().into_owned()),
                Component::ParentDir => {
                    out.pop();
                }
                _ => {}
            }
        }
        out
    }

    proptest! {
        #[test]
        fn normalization_matches_std_path(
            parts in prop::collection::vec(prop::sample::select(vec!["a", "b", "..", ".", "", "p"]), 0..10),
            absolute in any::<bool>(),
        ) {
            let raw = format!("{}{}", if absolute { "/" } else { "" }, parts.join("/"));
            let full = if raw.starts_with('/') { raw.clone() } else { format!("/p/{raw}") };
            let got = resolve("/p", None, &raw);
            prop_assert!(got.iter().all(|s| !s.is_empty() && *s != "." && *s != ".."));
            prop_assert_eq!(got, reference(&full));
        }
    }
}

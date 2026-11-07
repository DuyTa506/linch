//! Wildcard matching shared by bash and path rules.
//!
//! Both matchers are iterative and at worst quadratic: a hostile command or
//! path cannot make a match recurse deeply or backtrack exponentially.

/// Whether all of `text` matches `pattern`. `*` matches any run of
/// characters and `?` any one character. With `classes`, `[abc]`, `[a-z]`
/// and `[!a-z]` match one character in (or not in) the set, and a `[` with
/// no closing `]` is a literal; without it every `[` is a literal.
pub(super) fn wildcard(pattern: &str, text: &str, classes: bool) -> bool {
    let pat: Vec<char> = pattern.chars().collect();
    let text: Vec<char> = text.chars().collect();
    let (mut p, mut t) = (0, 0);
    // After a mismatch, retry from the last `*` with one more character
    // consumed by it. Earlier stars never need revisiting.
    let mut star: Option<(usize, usize)> = None;
    while t < text.len() {
        if p < pat.len() {
            if pat[p] == '*' {
                star = Some((p + 1, t));
                p += 1;
                continue;
            }
            let step = match pat[p] {
                '?' => Some(1),
                '[' if classes => match class(&pat[p..], text[t]) {
                    Some((hit, len)) => hit.then_some(len),
                    None => (text[t] == '[').then_some(1),
                },
                c => (c == text[t]).then_some(1),
            };
            if let Some(len) = step {
                p += len;
                t += 1;
                continue;
            }
        }
        let Some((after, from)) = star else {
            return false;
        };
        p = after;
        t = from + 1;
        star = Some((after, from + 1));
    }
    pat[p..].iter().all(|&c| c == '*')
}

/// Reads the class at the start of `pat` (which begins with `[`). Returns
/// whether `c` belongs to it and how many pattern characters it spans, or
/// `None` when it has no closing `]`. A `]` first in the set is a member,
/// and a `-` next to the closing `]` is a literal, as in `fnmatch`.
fn class(pat: &[char], c: char) -> Option<(bool, usize)> {
    let negate = pat.get(1) == Some(&'!');
    let first = if negate { 2 } else { 1 };
    let mut i = first;
    let mut hit = false;
    loop {
        let lo = *pat.get(i)?;
        if lo == ']' && i > first {
            return Some((hit != negate, i + 1));
        }
        match (pat.get(i + 1), pat.get(i + 2)) {
            (Some('-'), Some(&hi)) if hi != ']' => {
                hit |= lo <= c && c <= hi;
                i += 3;
            }
            _ => {
                hit |= lo == c;
                i += 1;
            }
        }
    }
}

/// Whether path `text` matches `pattern`, both given as segments. A `**`
/// segment spans zero or more segments, or one or more when it is the last
/// one (`a/**` matches what is inside `a`, not `a` itself). Any other
/// segment matches exactly one text segment with `*` and `?` wildcards.
pub(super) fn segments_match(pattern: &[&str], text: &[&str]) -> bool {
    // reach[j]: the pattern so far matches the first j text segments.
    let mut reach = vec![false; text.len() + 1];
    reach[0] = true;
    for (i, &segment) in pattern.iter().enumerate() {
        let mut next = vec![false; text.len() + 1];
        if segment == "**" {
            let last = i + 1 == pattern.len();
            let mut seen = false;
            for j in 0..=text.len() {
                if !last {
                    seen |= reach[j];
                }
                next[j] = seen;
                if last {
                    seen |= reach[j];
                }
            }
        } else {
            for j in 0..text.len() {
                next[j + 1] = reach[j] && wildcard(segment, text[j], false);
            }
        }
        reach = next;
    }
    reach[text.len()]
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::*;

    #[test]
    fn wildcard_follows_fnmatch() {
        assert!(wildcard("git *", "git status", true));
        assert!(wildcard("*", "", true));
        assert!(wildcard("a*b*c", "aXbYbZc", true));
        assert!(!wildcard("a*b*c", "aXbYbZ", true));
        assert!(wildcard("?x", "éx", true));
        assert!(wildcard("[a-c]x", "bx", true));
        assert!(!wildcard("[!a-c]x", "bx", true));
        assert!(wildcard("[]]", "]", true));
        assert!(wildcard("[a-]", "-", true));
        assert!(wildcard("[unterminated", "[unterminated", true));
        assert!(!wildcard("[ab]", "a", false));
        assert!(wildcard("[ab]", "[ab]", false));
    }

    #[test]
    fn wildcard_is_fast_on_hostile_input() {
        let text = "a".repeat(5_000);
        assert!(!wildcard(&format!("{}b", "*a".repeat(30)), &text, true));
    }

    #[test]
    fn segment_globs() {
        let m = |p: &str, t: &str| {
            let p: Vec<&str> = p.split('/').collect();
            let t: Vec<&str> = t.split('/').collect();
            segments_match(&p, &t)
        };
        assert!(m("config/*.json", "config/app.json"));
        assert!(!m("config/*.json", "config/prod/secrets.json"));
        assert!(m("config/**/*.json", "config/app.json"));
        assert!(m("config/**/*.json", "config/prod/x/secrets.json"));
        assert!(m("secrets/**", "secrets/a/b"));
        assert!(!m("secrets/**", "secrets"));
        assert!(m("**/*.rs", "lib.rs"));
        assert!(m("a/**/**/b", "a/b"));
        assert!(!m("a/?", "a/bc"));
    }

    proptest! {
        #[test]
        fn star_matches_any_text(text in any::<String>()) {
            prop_assert!(wildcard("*", &text, true));
            let pattern = format!("*{text}");
            prop_assert!(wildcard(&pattern, &text, false));
        }

        #[test]
        fn literal_patterns_match_only_themselves(a in "[a-z/.]{0,12}", b in "[a-z/.]{0,12}") {
            prop_assert_eq!(wildcard(&a, &b, true), a == b);
        }
    }
}

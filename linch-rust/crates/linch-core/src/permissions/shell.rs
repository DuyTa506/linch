//! Shell command analysis for bash rules: a word tokenizer, the
//! composite-command splitter, and a detector for syntax neither models.
//!
//! The model is small on purpose: words, single and double quotes,
//! backslash escapes, the list operators `;` `&&` `||` `|`, and redirections
//! (`>`, `2>&1`). Anything that can run a command the splitter would
//! not see as its own segment is reported by [`has_unparsed_construct`], and
//! the engine applies its floor to it instead of trusting the segments.
//! Words are separated by space and tab only, as in bash.

use super::glob::wildcard;

/// Whether bash separates words at `c`.
fn is_blank(c: char) -> bool {
    c == ' ' || c == '\t'
}

/// `text` without leading and trailing spaces and tabs.
pub fn trim_blanks(text: &str) -> &str {
    text.trim_matches(is_blank)
}

/// Split one command segment into words, removing quotes and backslash
/// escapes the way the shell does. Expansions stay literal text.
pub fn tokenize(input: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut in_token = false;
    let mut quote: Option<char> = None;
    let mut chars = input.chars();
    while let Some(c) = chars.next() {
        match quote {
            Some('\'') if c == '\'' => quote = None,
            Some('\'') => current.push(c),
            Some(_) if c == '"' => quote = None,
            Some(_) if c == '\\' => match chars.clone().next() {
                // Inside double quotes only these bytes lose their
                // backslash; before any other byte the shell keeps it.
                Some(n @ ('$' | '`' | '"' | '\\')) => {
                    current.push(n);
                    chars.next();
                }
                Some('\n') => {
                    chars.next();
                }
                _ => current.push(c),
            },
            Some(_) => current.push(c),
            None if is_blank(c) => {
                if in_token {
                    tokens.push(std::mem::take(&mut current));
                    in_token = false;
                }
            }
            None => {
                in_token = true;
                match c {
                    '\'' | '"' => quote = Some(c),
                    '\\' => current.push(chars.next().unwrap_or('\\')),
                    _ => current.push(c),
                }
            }
        }
    }
    if in_token {
        tokens.push(current);
    }
    tokens
}

/// Split a command at unquoted, unescaped `;`, `&&`, `||` and `|`. Segments
/// are returned untrimmed and may be empty.
pub fn split_composite(input: &str) -> Vec<&str> {
    let bytes = input.as_bytes();
    let mut segments = Vec::new();
    let (mut start, mut i) = (0, 0);
    let mut quote: Option<u8> = None;
    let mut escaped = false;
    while i < bytes.len() {
        let b = bytes[i];
        if escaped {
            escaped = false;
        } else if quote != Some(b'\'') && b == b'\\' {
            escaped = true;
        } else if let Some(q) = quote {
            if b == q {
                quote = None;
            }
        } else if b == b'\'' || b == b'"' {
            quote = Some(b);
        } else {
            let op = match (b, bytes.get(i + 1)) {
                (b'&', Some(b'&')) | (b'|', Some(b'|')) => 2,
                (b';' | b'|', _) => 1,
                _ => 0,
            };
            if op > 0 {
                // Operators are ASCII, so `i` is a char boundary.
                segments.push(&input[start..i]);
                i += op;
                start = i;
                continue;
            }
        }
        i += 1;
    }
    segments.push(&input[start..]);
    segments
}

/// Whether `command` uses shell syntax that [`split_composite`] cannot
/// model: a background `&` (`&>` included: `sh` reads it as `&` then `>`), an
/// unquoted newline or carriage return, a line continuation, a comment
/// (an unquoted `#` at the start of a word), a here-document (`<<`, `<<-`),
/// the clobber redirect `>|`, command substitution (`` ` ``, `$(`), process
/// substitution or subshell parentheses, ANSI-C quoting (`$'...'`, whose `\'`
/// the quote tracking would misread), or an unbalanced quote or trailing
/// backslash, or a `${...}` inside double quotes whose body holds a quote, a
/// backslash or a nested `${`. `&` is a redirection only right after an unescaped, unquoted
/// `<` or `>` and names a descriptor (`2>&1`, `>&2`, `<&3`, `>&-`): what
/// `>&file` means differs between shells. The shell differs per backend (dash,
/// bash, busybox), so what one of them reads differently is reported. A `$`
/// expansion in command position (`$a -rf x`) or `env -S` is reported too:
/// the command is not in the text.
pub fn has_unparsed_construct(command: &str) -> bool {
    let b = command.as_bytes();
    let mut quote: Option<u8> = None;
    let mut escaped = false;
    // Index just past the last unescaped, unquoted `<` or `>`.
    let mut redirect_end = 0;
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        let next = b.get(i + 1).copied();
        if escaped {
            if c == b'\r' || c == b'\n' {
                return true;
            }
            escaped = false;
        } else if quote != Some(b'\'') && c == b'\\' {
            escaped = true;
        } else if quote == Some(b'\'') {
            if c == b'\'' {
                quote = None;
            }
        } else if quote == Some(b'"') {
            if c == b'"' {
                quote = None;
            } else if c == b'`' || (c == b'$' && next == Some(b'(')) {
                return true;
            } else if c == b'$' && next == Some(b'{') && quotes_in_expansion(&b[i + 2..]) {
                return true;
            }
        } else {
            match c {
                b'\'' | b'"' => quote = Some(c),
                b'\r' | b'\n' | b'`' | b'(' | b')' => return true,
                b'$' if matches!(next, Some(b'(' | b'\'')) => return true,
                b'#' if i == 0
                    || matches!(b[i - 1], b' ' | b'\t' | b';' | b'&' | b'|' | b'<' | b'>') =>
                {
                    return true;
                }
                b'<' if b[i..].starts_with(b"<<<") => {
                    // A here-string: its word is an ordinary word.
                    i += 3;
                    redirect_end = i;
                    continue;
                }
                b'<' if next == Some(b'<') => return true,
                b'>' if next == Some(b'|') => return true,
                b'<' | b'>' => redirect_end = i + 1,
                // `2>&1`, `>&2`, `<&3`: a redirection.
                b'&' if redirect_end == i && matches!(next, Some(b'0'..=b'9' | b'-')) => {}
                b'&' if next == Some(b'&') => {
                    i += 2;
                    continue;
                }
                b'&' => return true,
                _ => {}
            }
        }
        i += 1;
    }
    escaped
        || quote.is_some()
        || split_composite(command)
            .into_iter()
            .any(command_is_not_in_the_text)
}

/// Whether the body of a `${` inside double quotes, up to its first `}`,
/// holds a quote, a backslash or a nested `${`. The shell reads those on
/// their own (`"${x#'"'}"` quotes a `"` inside the pattern), which the quote
/// tracking cannot follow, so the operators are not modelled.
fn quotes_in_expansion(after: &[u8]) -> bool {
    let end = after.iter().position(|&c| c == b'}').unwrap_or(after.len());
    let body = &after[..end];
    body.iter().any(|c| matches!(c, b'\'' | b'"' | b'\\')) || body.windows(2).any(|w| w == b"${")
}

/// End of the first word of `text`: the first blank outside quotes and
/// escapes, or its length.
fn word_end(text: &str) -> usize {
    let b = text.as_bytes();
    let mut quote: Option<u8> = None;
    let mut i = 0;
    while i < b.len() {
        match (quote, b[i]) {
            (Some(q), c) if c == q => quote = None,
            (Some(b'\''), _) => {}
            (_, b'\\') => i += 1,
            (Some(_), _) => {}
            (None, c @ (b'\'' | b'"')) => quote = Some(c),
            (None, b' ' | b'\t') => return i,
            _ => {}
        }
        i += 1;
    }
    b.len()
}

/// `word` with its quotes and backslashes removed.
fn unquote(word: &str) -> String {
    tokenize(word).concat()
}

/// Whether `word` is `NAME=value` or `NAME+=value`.
fn is_assignment(word: &str) -> bool {
    let name = word.split('=').next().unwrap_or("");
    let name = name.strip_suffix('+').unwrap_or(name);
    word.contains('=')
        && name.starts_with(|c: char| c.is_ascii_alphabetic() || c == '_')
        && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// Whether `word` is an assignment to an array element (`a[0]=1`).
fn is_array_assignment(word: &str) -> bool {
    word.split_once('=').is_some_and(|(name, _)| {
        name.starts_with(|c: char| c.is_ascii_alphabetic() || c == '_') && name.contains('[')
    })
}

/// Whether `word` is a redirection with a descriptor variable (`{fd}>log`).
fn is_fd_redirection(word: &str) -> bool {
    word.strip_prefix('{')
        .and_then(|rest| rest.split_once('}'))
        .is_some_and(|(name, after)| {
            !name.is_empty()
                && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
                && after.starts_with(['<', '>'])
        })
}

/// Words that precede a command without being it.
const RESERVED: &[&str] = &[
    "if", "then", "else", "elif", "do", "while", "until", "!", "{",
];
/// Commands that run their arguments as a command, and may take options.
const WRAPPERS: &[&str] = &["time", "exec", "command", "builtin", "env"];

/// What a segment's command is once the words in front of it are removed.
struct Command<'a> {
    /// The segment from the command word on.
    text: &'a str,
    /// Whether the shell reads the command from text the segment does not
    /// hold as words (`env -S 'rm x'`).
    opaque: bool,
}

/// How a wrapper option takes part in finding the command.
enum WrapperOption {
    Plain,
    /// Takes the next word as its argument (`env -u NAME`).
    Takes,
    /// Splits a string into the command (`env -S`).
    Split,
    /// Only looks the command up (`command -v`).
    Query,
}

fn wrapper_option(wrapper: &str, option: &str) -> WrapperOption {
    match (wrapper, option) {
        ("env", "-u" | "-C" | "--unset" | "--chdir")
        | ("time", "-f" | "-o" | "--format" | "--output")
        | ("exec", "-a") => WrapperOption::Takes,
        ("env", o) if o.starts_with("-S") || o.starts_with("--split-string") => {
            WrapperOption::Split
        }
        ("command", o) if !o.starts_with("--") && o.contains(['v', 'V']) => WrapperOption::Query,
        _ => WrapperOption::Plain,
    }
}

fn strip_prefix(segment: &str) -> Command<'_> {
    let mut rest = trim_blanks(segment);
    // The wrapper whose options are being read, and the text from it on.
    let mut wrapper: Option<(String, &str)> = None;
    loop {
        let end = word_end(rest);
        let word = &rest[..end];
        let operator = word.trim_start_matches(|c: char| c.is_ascii_digit());
        if is_array_assignment(word) || is_fd_redirection(word) {
            return Command {
                text: rest,
                opaque: true,
            };
        }
        let assignment = is_assignment(word);
        let name = if assignment {
            String::new()
        } else {
            unquote(word)
        };
        let mut skip = 1;
        if assignment {
        } else if RESERVED.contains(&name.as_str()) {
            wrapper = None;
        } else if WRAPPERS.contains(&name.as_str()) {
            wrapper = Some((name, rest));
        } else if let Some((wrapper, at)) = &wrapper
            && word.starts_with('-')
        {
            match wrapper_option(wrapper, &name) {
                WrapperOption::Plain => {}
                WrapperOption::Takes => skip = 2,
                WrapperOption::Split => {
                    return Command {
                        text: rest,
                        opaque: true,
                    };
                }
                WrapperOption::Query => {
                    return Command {
                        text: at,
                        opaque: false,
                    };
                }
            }
        } else if operator.starts_with(['<', '>']) {
            // A bare operator (`>`, `2>&`) takes the next word as its target.
            if operator.trim_start_matches(['<', '>', '&', '|']).is_empty() {
                skip = 2;
            }
        } else {
            return Command {
                text: rest,
                opaque: matches!(name.as_str(), "function" | "coproc"),
            };
        }
        for _ in 0..skip {
            let end = word_end(rest);
            rest = trim_blanks(&rest[end..]);
        }
    }
}

/// `segment` without the words in front of its command, which may be quoted
/// or escaped (`\env`): `NAME=value` assignments, redirections (`>f`,
/// `2>&1`, `< in`), reserved words (`if`, `then`, `else`, `elif`, `do`,
/// `while`, `until`, `!`, `{`) and the wrappers `time`, `exec`, `command`,
/// `builtin` and `env` with their options (`env -u NAME`, `time -o FILE`,
/// `exec -a NAME` take an argument; `command -v` only looks a name up and is
/// kept whole). Deny and ask rules are matched against this form as well, so
/// a prefix cannot hide the command. Allow rules never use it: `X=1 echo` is
/// not `echo`. Name-based and best effort: `/bin/rm`, `sudo rm` and
/// `sh -c 'rm x'` are other commands.
pub fn strip_command_prefix(segment: &str) -> &str {
    strip_prefix(segment).text
}

/// Whether the command of `segment` is not in its text: the shell reads it
/// from a string (`env -S`), or the segment starts with a bash construct that
/// is not modelled (`function`, `coproc`, `a[0]=1`, `{fd}>log`), or its word
/// (after [`strip_command_prefix`]) holds a `$` expansion outside single
/// quotes or a brace expansion (`{rm,-rf}`).
fn command_is_not_in_the_text(segment: &str) -> bool {
    let command = strip_prefix(segment);
    if command.opaque {
        return true;
    }
    let rest = command.text;
    let b = &rest.as_bytes()[..word_end(rest)];
    let mut quote: Option<u8> = None;
    let mut escaped = false;
    let (mut open, mut last) = (false, 0);
    for &c in b {
        if escaped {
            escaped = false;
        } else if c == b'\\' && quote != Some(b'\'') {
            escaped = true;
        } else if let Some(q) = quote {
            if c == q {
                quote = None;
            } else if c == b'$' && q == b'"' {
                return true;
            }
        } else if c == b'\'' || c == b'"' {
            quote = Some(c);
        } else if c == b'$' || (open && (c == b',' || (c == b'.' && last == b'.'))) {
            return true;
        } else if c == b'{' {
            open = true;
        }
        last = c;
    }
    false
}

/// Whether bash rule `pattern` matches one command segment: as a glob over
/// the whole trimmed segment (`*` spans anything, `[...]` classes work), or
/// because the pattern's words are a prefix of the segment's words.
pub fn pattern_matches(pattern: &str, segment: &str) -> bool {
    let segment = trim_blanks(segment);
    if wildcard(pattern, segment, true) {
        return true;
    }
    let prefix = tokenize(pattern);
    !prefix.is_empty() && tokenize(segment).starts_with(&prefix)
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::*;

    #[test]
    fn tokenizer_removes_quotes_and_escapes() {
        assert_eq!(
            tokenize("git  commit -m 'a b'"),
            ["git", "commit", "-m", "a b"]
        );
        assert_eq!(tokenize(r#"echo "x \" y" a\ b"#), ["echo", "x \" y", "a b"]);
        assert_eq!(tokenize(r"echo 'a\' b"), ["echo", r"a\", "b"]);
        assert_eq!(tokenize("''"), [""]);
        // Inside double quotes a backslash escapes only `$`, backtick, `"`,
        // `\` and newline; before any other byte the shell keeps it.
        assert_eq!(
            tokenize(r#"echo "a\nb" "c\$d" "e\\f" "g\`h""#),
            ["echo", r"a\nb", "c$d", r"e\f", "g`h"]
        );
        assert_eq!(tokenize("tail\\"), ["tail\\"]);
        assert!(tokenize(" \t ").is_empty());
        // Only space and tab separate words, as in bash.
        assert_eq!(tokenize("a\u{a0}b"), ["a\u{a0}b"]);
    }

    #[test]
    fn splitter_honors_quotes_and_escapes() {
        assert_eq!(
            split_composite("a; b && c || d | e"),
            ["a", " b ", " c ", " d ", " e"]
        );
        assert_eq!(
            split_composite("echo 'a;b' \"c|d\" e\\;f"),
            ["echo 'a;b' \"c|d\" e\\;f"]
        );
        assert_eq!(split_composite(";;"), ["", "", ""]);
        assert_eq!(split_composite("é;ü"), ["é", "ü"]);
    }

    #[test]
    fn detector_flags_what_the_splitter_cannot_model() {
        for command in [
            "a & b",
            "a &",
            "a\nb",
            "a\rb",
            "a \\\nb",
            "echo $(x)",
            "echo \"$(x)\"",
            "echo `x`",
            "echo \"`x`\"",
            "cat <(x)",
            "(cd a)",
            "echo 'open",
            "echo \"open",
            "echo trailing\\",
            "a |& b",
            "echo \"${x#'a'}\"",
            "echo \"${x#'\"'}\" ; b ; echo \\'",
            "echo \"${x:-${y}}\"",
            "echo \"${x#\\\"}\"",
            "a &> log",
            "echo hi &>/tmp/f touch x",
            "echo \\>& x",
            "echo \\<& x",
            "echo >& file",
            "echo >&$fd",
            "echo '>'& x",
            "echo hi #'\ntouch x\n#'",
            "echo hi #",
            "echo hi;#x",
            "echo hi|#x",
            "#x",
            "a >| f",
            "$a -rf x",
            "x=1 $a",
            "a | ${b}",
            "a; \"$b\"",
            "a <<EOF",
            "a <<-EOF",
            "a <<'EOF'",
            "echo \\$(x)",
            "echo $'\\'' ; rm -rf / ; '\\'",
        ] {
            assert!(has_unparsed_construct(command), "{command:?}");
        }
        for command in [
            "a && b",
            "a 2>&1",
            "a <&3",
            "a >&-",
            "a 1>&2 2>&1",
            "a <<< word",
            "a <<<\"x;y\"",
            "a $b",
            "x=$a b",
            "'$a' b",
            "a#b",
            "echo $#",
            "echo ${#x} ${x#p}",
            "echo \\# \"#\" '#'",
            "echo \"a\\x\"",
            "echo \"a\\\"b\"",
            "{ a; }",
            "printf '&'",
            "echo \"${x#a}\" \"${y:-b}\" ${z#'\"'}",
            "printf '$(x)'",
            "echo \"\\$(x)\"",
            "echo \"a\nb\"",
            "echo \\$'x'",
            "echo \"$'x'\"",
        ] {
            assert!(!has_unparsed_construct(command), "{command:?}");
        }
    }

    #[test]
    fn ansi_c_quoting_cannot_hide_a_segment() {
        // Bash reads `$'\''` as one quoted `'`, so `rm` runs; the quote
        // tracker sees the `;`s as quoted and would return one segment.
        let command = "echo $'\\'' ; rm -rf / ; '\\'";
        assert_eq!(split_composite(command).len(), 1);
        assert!(has_unparsed_construct(command));
    }

    #[test]
    fn prefix_words_are_stripped_before_the_command() {
        let cases = [
            ("rm -rf x", "rm -rf x"),
            ("  X=1 rm x", "rm x"),
            ("X=1 Y='a b' Z+=\"c d\" rm x", "rm x"),
            ("if rm x", "rm x"),
            ("then ! { rm x", "rm x"),
            ("time -p command -- rm x", "rm x"),
            ("env -i X=1 rm x", "rm x"),
            (">log 2>&1 rm x", "rm x"),
            ("> log < in rm x", "rm x"),
            ("2>&1 rm x", "rm x"),
            ("X=1", ""),
            ("then", ""),
            ("echo if rm x", "echo if rm x"),
            ("X='a b rm x", ""),
            ("\"X\"=1 rm x", "\"X\"=1 rm x"),
            ("1x=1 rm x", "1x=1 rm x"),
            ("rm -rf x", "rm -rf x"),
        ];
        for (segment, expected) in cases {
            assert_eq!(strip_command_prefix(segment), expected, "{segment:?}");
        }
    }

    #[test]
    fn patterns_match_by_glob_or_word_prefix() {
        let cases = [
            ("rm -rf /tmp", "rm -rf /tmp", true),
            ("rm -rf /tmp", "rm -rf /tmp/../etc", false),
            ("ls", "ls", true),
            ("ls", "lsof -i :8080", false),
            ("npm install", "npm install lodash", true),
            ("git *", "git status", true),
            ("git *", "  git status\t", true),
            ("rm -rf /", "rm -rf /", true),
            ("rm [!a]*", "rm b", true),
            ("rm [!a]*", "rm a", false),
            ("", "ls", false),
        ];
        for (pattern, segment, expected) in cases {
            assert_eq!(
                pattern_matches(pattern, segment),
                expected,
                "{pattern:?} vs {segment:?}"
            );
        }
    }

    proptest! {
        #[test]
        fn analysis_never_panics(text in any::<String>(), pattern in any::<String>()) {
            let _ = tokenize(&text);
            let _ = split_composite(&text);
            let _ = has_unparsed_construct(&text);
            let _ = pattern_matches(&pattern, &text);
        }

        #[test]
        fn shell_alphabet_never_panics(text in "[a-z \t&|;<>()$`'\"\\\\\n*?\\[\\]!é]{0,40}") {
            let segments = split_composite(&text);
            let joined: usize = segments.iter().map(|s| s.len()).sum();
            prop_assert!(joined <= text.len());
            let _ = tokenize(&text);
            let _ = has_unparsed_construct(&text);
            let _ = pattern_matches(&text, &text);
        }

        #[test]
        fn plain_lists_split_into_their_commands(
            words in prop::collection::vec("[a-z]{1,5}( [a-z]{1,5}){0,2}", 1..6),
            ops in prop::collection::vec(prop::sample::select(vec![";", "&&", "||", "|"]), 5),
        ) {
            let mut command = words[0].clone();
            for (word, op) in words[1..].iter().zip(&ops) {
                command.push_str(&format!(" {op} {word}"));
            }
            prop_assert!(!has_unparsed_construct(&command));
            let segments: Vec<&str> = split_composite(&command).into_iter().map(trim_blanks).collect();
            prop_assert_eq!(segments, words.iter().map(String::as_str).collect::<Vec<_>>());
        }
    }
}

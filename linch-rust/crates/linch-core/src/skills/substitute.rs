//! [`substitute`]: a skill body with its arguments and placeholders filled.

use super::Skill;

/// The body of `skill` with its placeholders filled, after a line naming
/// the skill's directory.
///
/// `$ARGUMENTS` becomes `args` as given; `${SKILL_DIR}` and
/// `${LINCH_SKILL_DIR}` the skill's directory; `${LINCH_SESSION_ID}`
/// `session_id`; and `$name` of each named argument the token at its
/// position in [`split_args`]`(args)`, or nothing. A `$`-placeholder
/// without braces only matches when no letter, digit or `_` follows it.
/// One pass, longest placeholder first: a substituted value is never
/// expanded again, and no shell quoting is added.
pub fn substitute(skill: &Skill, args: &str, session_id: &str) -> String {
    let tokens = split_args(args);
    let mut replacements: Vec<(String, &str)> = vec![
        ("$ARGUMENTS".into(), args),
        ("${SKILL_DIR}".into(), &skill.dir),
        ("${LINCH_SKILL_DIR}".into(), &skill.dir),
        ("${LINCH_SESSION_ID}".into(), session_id),
    ];
    for (at, name) in skill.arguments.iter().enumerate() {
        let value = tokens.get(at).map_or("", String::as_str);
        replacements.push((format!("${name}"), value));
    }
    replacements.sort_by_key(|(placeholder, _)| std::cmp::Reverse(placeholder.len()));
    let body = &skill.body;
    let mut out = String::with_capacity(body.len());
    let mut rest = body.as_str();
    'scan: while let Some(at) = rest.find('$') {
        out.push_str(&rest[..at]);
        rest = &rest[at..];
        for (placeholder, value) in &replacements {
            if let Some(after) = rest.strip_prefix(placeholder.as_str())
                && (placeholder.ends_with('}') || !after.starts_with(is_word))
            {
                out.push_str(value);
                rest = after;
                continue 'scan;
            }
        }
        out.push('$');
        rest = &rest[1..];
    }
    out.push_str(rest);
    format!("Skill base directory: {}\n\n{out}", skill.dir)
}

fn is_word(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

/// Split shell-like arguments on whitespace, honouring single quotes,
/// double quotes and backslash escapes; quotes and escapes are removed.
/// An unclosed quote runs to the end.
pub fn split_args(raw: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut in_token = false;
    let mut quote: Option<char> = None;
    let mut chars = raw.chars().peekable();
    while let Some(c) = chars.next() {
        match quote {
            Some('\'') => {
                if c == '\'' {
                    quote = None;
                } else {
                    current.push(c);
                }
                continue;
            }
            Some(_) => {
                match c {
                    '"' => quote = None,
                    '\\' if chars.peek().is_some() => current.extend(chars.next()),
                    _ => current.push(c),
                }
                continue;
            }
            None => {}
        }
        if c.is_whitespace() {
            if in_token {
                tokens.push(std::mem::take(&mut current));
                in_token = false;
            }
            continue;
        }
        in_token = true;
        match c {
            '\'' | '"' => quote = Some(c),
            '\\' if chars.peek().is_some() => current.extend(chars.next()),
            _ => current.push(c),
        }
    }
    if quote.is_some() || in_token {
        tokens.push(current);
    }
    tokens
}

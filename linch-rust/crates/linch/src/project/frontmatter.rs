//! The YAML frontmatter at the start of a markdown definition file.

use yaml_rust2::{Yaml, YamlLoader};

/// The detail for a file that does not start with a `---` line.
pub(super) const MISSING: &str = "missing frontmatter (file must start with `---`)";

/// Why a file's frontmatter is unusable.
pub(super) enum Error {
    /// The file does not start with a `---` line closed by another.
    Missing,
    /// The YAML does not parse or is not one mapping; the detail.
    Invalid(String),
}

/// The frontmatter mapping of `raw` and the markdown body after it.
pub(super) fn parse(raw: &str) -> Result<(Yaml, &str), Error> {
    let (yaml, body) = split(raw).ok_or(Error::Missing)?;
    let invalid = |detail: &str| Error::Invalid(detail.to_owned());
    let mut docs = YamlLoader::load_from_str(yaml)
        .map_err(|error| Error::Invalid(format!("YAML parse error: {error}")))?;
    if docs.len() > 1 {
        return Err(invalid("YAML parse error: expected a single document"));
    }
    match docs.pop() {
        None | Some(Yaml::Null) => Err(invalid("frontmatter is empty")),
        Some(doc @ Yaml::Hash(_)) => Ok((doc, body)),
        Some(_) => Err(invalid("frontmatter must be a YAML mapping")),
    }
}

/// Split `---\n<yaml>\n---` off the start of `raw`, with `\n` or `\r\n`
/// line ends: the YAML ends at the first line that starts with `---`, and
/// the body starts after that line's end.
fn split(raw: &str) -> Option<(&str, &str)> {
    let rest = (raw.strip_prefix("---\n")).or_else(|| raw.strip_prefix("---\r\n"))?;
    let end = rest.find("\n---")?;
    let yaml = &rest[..end];
    let yaml = yaml.strip_suffix('\r').unwrap_or(yaml);
    let body = &rest[end + "\n---".len()..];
    let body = (body.strip_prefix("\r\n"))
        .or_else(|| body.strip_prefix('\n'))
        .unwrap_or(body);
    Some((yaml, body))
}

/// The value at `key`, or `None` when it is absent or null.
pub(super) fn get<'a>(doc: &'a Yaml, key: &str) -> Option<&'a Yaml> {
    match &doc[key] {
        Yaml::BadValue | Yaml::Null => None,
        value => Some(value),
    }
}

#[cfg(test)]
mod tests {
    use super::split;

    #[test]
    fn the_frontmatter_ends_at_the_first_dash_line() {
        assert_eq!(split("---\na: 1\n---\nbody"), Some(("a: 1", "body")));
        assert_eq!(split("---\r\na: 1\r\n---\r\nbody"), Some(("a: 1", "body")));
        assert_eq!(split("---\na: 1\n---"), Some(("a: 1", "")));
        assert_eq!(split("---\n\n---\n"), Some(("", "")));
        assert_eq!(split("---\na\n---\nb\n---\nc"), Some(("a", "b\n---\nc")));
        assert_eq!(split("a: 1\n---\n"), None);
        assert_eq!(split("---\na: 1\n"), None);
        assert_eq!(split(" ---\na\n---\n"), None);
    }
}

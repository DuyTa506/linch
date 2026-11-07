//! Policy validation: reject rules that could never match as written.

use super::path;
use super::{PermissionPolicy, PolicyError, Rule, RuleProblem};

/// `mcp__<server>__` for a `mcp__<server>__*` pattern whose server name is
/// unambiguous: `[A-Za-z0-9_-]+`, no `__`, no leading or trailing `_`.
pub(super) fn mcp_server_prefix(pattern: &str) -> Option<&str> {
    let prefix = pattern.strip_suffix('*')?;
    let server = prefix.strip_prefix("mcp__")?.strip_suffix("__")?;
    let valid = !server.is_empty()
        && !server.contains("__")
        && !server.starts_with('_')
        && !server.ends_with('_')
        && server
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-');
    valid.then_some(prefix)
}

pub(super) fn validate(policy: &PermissionPolicy) -> Result<(), PolicyError> {
    if !policy.project_root.starts_with('/') {
        return Err(PolicyError::RelativeRoot {
            root: policy.project_root.clone(),
        });
    }
    let paths = policy
        .path_fields
        .iter()
        .map(|(t, f)| (t, f.field.as_str()));
    let commands = policy.command_fields.iter().map(|(t, f)| (t, f.as_str()));
    if let Some((tool, _)) = paths.chain(commands).find(|(_, f)| f.trim().is_empty()) {
        return Err(PolicyError::BlankField { tool: tool.clone() });
    }
    for layer in &policy.layers {
        for (index, rule) in layer.rules.iter().enumerate() {
            check_rule(policy, rule).map_err(|problem| PolicyError::Rule {
                layer: layer.name.clone(),
                index,
                problem,
            })?;
        }
    }
    Ok(())
}

fn check_rule(policy: &PermissionPolicy, rule: &Rule) -> Result<(), RuleProblem> {
    let blank = |value: Option<&str>, field| match value {
        Some(v) if v.trim().is_empty() => Err(RuleProblem::Blank { field }),
        _ => Ok(()),
    };
    let (role, unless_role) = rule.roles();
    blank(role, "role")?;
    blank(unless_role, "unless_role")?;
    match rule {
        Rule::Tool(r) => {
            let tool = r.tool.as_str();
            let named = !tool.contains('*') && !tool.trim().is_empty();
            if !(tool == "*" || named || mcp_server_prefix(tool).is_some()) {
                return Err(RuleProblem::ToolPattern {
                    pattern: r.tool.clone(),
                });
            }
            blank(r.skill.as_deref(), "skill")?;
            if r.skill.is_some() && tool != "Skill" && tool != "*" {
                return Err(RuleProblem::SkillTool);
            }
        }
        Rule::Path(r) => {
            if r.paths.is_empty() {
                return Err(RuleProblem::Empty { field: "paths" });
            }
            if let Some(p) = r.paths.iter().find(|p| !path::valid_pattern(p)) {
                return Err(RuleProblem::PathPattern { pattern: p.clone() });
            }
            if let Some(tools) = &r.tools {
                if tools.is_empty() {
                    return Err(RuleProblem::Empty { field: "tools" });
                }
                if let Some(t) = tools.iter().find(|t| path::field(policy, t).is_none()) {
                    return Err(RuleProblem::NoPathField { tool: t.clone() });
                }
            }
        }
        Rule::Bash(r) => {
            if r.patterns.is_empty() {
                return Err(RuleProblem::Empty { field: "patterns" });
            }
            if r.patterns.iter().any(|p| p.trim().is_empty()) {
                return Err(RuleProblem::Blank { field: "patterns" });
            }
        }
    }
    Ok(())
}

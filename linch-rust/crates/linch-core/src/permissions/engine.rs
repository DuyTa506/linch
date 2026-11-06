//! Policy evaluation: layers, the unparsed-command floor, and mode
//! defaults.

use crate::json::{JsonObject, JsonValue};
use crate::policy::{PermissionDecision, Principal};
use crate::tool::ToolScope;

use super::path;
use super::validate::{mcp_server_prefix, validate};
use super::{
    BashRule, PathRule, PermissionLayer, PermissionMode, PermissionPolicy, PolicyError, Rule,
    RuleDecision, ToolRule, shell,
};

/// One proposed tool call, as the permission engine sees it.
///
/// Paths in `input` are judged lexically; resolving symlinks and confining
/// what is really opened belongs to the execution-world file layer (P6).
#[derive(Debug, Clone, Copy)]
pub struct PermissionCall<'a> {
    /// Name of the tool being called.
    pub tool_name: &'a str,
    /// The tool's declared scope.
    pub scope: ToolScope,
    /// Validated tool input.
    pub input: &'a JsonObject,
    /// Working directory relative paths resolve against: relative to the
    /// project root when relative, the project root when absent.
    pub cwd: Option<&'a str>,
}

impl<'a> PermissionCall<'a> {
    /// Call running in the project root.
    pub fn new(tool_name: &'a str, scope: ToolScope, input: &'a JsonObject) -> Self {
        Self {
            tool_name,
            scope,
            input,
            cwd: None,
        }
    }
}

/// A validated [`PermissionPolicy`], ready to evaluate calls.
#[derive(Debug, Clone)]
pub struct PermissionEngine {
    policy: PermissionPolicy,
}

impl PermissionEngine {
    /// Validate `policy` for evaluation.
    ///
    /// # Errors
    ///
    /// [`PolicyError`] when the project root is relative, a field name is
    /// blank, or a rule could never match as written.
    pub fn new(policy: PermissionPolicy) -> Result<Self, PolicyError> {
        validate(&policy)?;
        Ok(Self { policy })
    }

    /// The policy this engine evaluates.
    pub fn policy(&self) -> &PermissionPolicy {
        &self.policy
    }

    /// Decide `call` for `principal` (no principal holds no roles). `Ask`
    /// is returned as-is; resolving it is the caller's job.
    pub fn evaluate(
        &self,
        call: &PermissionCall<'_>,
        principal: Option<&Principal>,
    ) -> PermissionDecision {
        let eval = Eval::new(&self.policy, *call, principal);
        if let Some(floor) = eval.unparsed_floor() {
            return floor.into_decision(call.tool_name);
        }
        let mut last = None;
        for layer in &self.policy.layers {
            match eval.layer(layer) {
                Some(hit) if hit.decision == RuleDecision::Deny => {
                    return hit.into_decision(call.tool_name);
                }
                Some(hit) => last = Some(hit),
                None => {}
            }
        }
        match last {
            Some(hit) => hit.into_decision(call.tool_name),
            None => eval.mode_default(),
        }
    }
}

/// A decision and what produced it, for the reason text.
struct Verdict<'a> {
    decision: RuleDecision,
    /// Rule kind and layer, or `None` for the unparsed-command floor.
    source: Option<(&'static str, &'a str)>,
}

impl Verdict<'_> {
    fn into_decision(self, tool: &str) -> PermissionDecision {
        let why = match self.source {
            Some((kind, layer)) => format!("{kind} rule in layer {layer:?}"),
            None => "shell syntax the policy cannot check".to_owned(),
        };
        match self.decision {
            RuleDecision::Allow => PermissionDecision::Allow {},
            RuleDecision::Deny => PermissionDecision::Deny {
                reason: format!("{tool} denied by {why}"),
            },
            RuleDecision::Ask => PermissionDecision::Ask {
                reason: Some(format!("{tool} needs approval: {why}")),
            },
        }
    }
}

/// One evaluation: the call plus what is derived from it once.
struct Eval<'a> {
    policy: &'a PermissionPolicy,
    call: PermissionCall<'a>,
    principal: Option<&'a Principal>,
    command: Option<&'a str>,
    unparsed: bool,
}

impl<'a> Eval<'a> {
    fn new(
        policy: &'a PermissionPolicy,
        call: PermissionCall<'a>,
        principal: Option<&'a Principal>,
    ) -> Self {
        let field = match policy.command_fields.get(call.tool_name) {
            Some(field) => Some(field.as_str()),
            None => (call.tool_name == "Bash").then_some("command"),
        };
        let command = field.and_then(|f| call.input.get(f)?.as_str());
        Self {
            policy,
            call,
            principal,
            command,
            unparsed: command.is_some_and(shell::has_unparsed_construct),
        }
    }

    fn applies(&self, rule: &Rule) -> bool {
        let (role, unless_role) = rule.roles();
        let has = |r: &str| self.principal.is_some_and(|p| p.has_role(r));
        role.is_none_or(has) && unless_role.is_none_or(|r| !has(r))
    }

    /// For a command the splitter cannot model: a matching deny rule in any
    /// layer denies it, and any other applicable bash deny rule asks.
    fn unparsed_floor(&self) -> Option<Verdict<'a>> {
        let command = self.command.filter(|_| self.unparsed)?;
        let mut bash_deny = false;
        for layer in &self.policy.layers {
            let denies = layer
                .rules
                .iter()
                .filter(|r| r.decision() == RuleDecision::Deny && self.applies(r));
            for rule in denies {
                let (kind, matched) = match rule {
                    Rule::Tool(r) => ("tool", self.tool_matches(r)),
                    Rule::Bash(r) => {
                        bash_deny = true;
                        let m = r
                            .patterns
                            .iter()
                            .any(|p| shell::pattern_matches(p, command));
                        ("bash", m)
                    }
                    Rule::Path(_) => continue,
                };
                if matched {
                    return Some(Verdict {
                        decision: RuleDecision::Deny,
                        source: Some((kind, &layer.name)),
                    });
                }
            }
        }
        bash_deny.then_some(Verdict {
            decision: RuleDecision::Ask,
            source: None,
        })
    }

    /// First-match decision of one layer, or `None` when it abstains.
    fn layer(&self, layer: &'a PermissionLayer) -> Option<Verdict<'a>> {
        let hit = |decision, kind| Verdict {
            decision,
            source: Some((kind, layer.name.as_str())),
        };
        let mut pooled = false;
        for (i, rule) in layer.rules.iter().enumerate() {
            if !self.applies(rule) {
                continue;
            }
            match rule {
                Rule::Tool(r) if self.tool_matches(r) => return Some(hit(r.decision, "tool")),
                Rule::Path(r) if self.path_matches(r) => return Some(hit(r.decision, "path")),
                Rule::Bash(_) if !pooled => {
                    pooled = true;
                    if let Some(decision) = self.bash_pool(&layer.rules[i..]) {
                        return Some(hit(decision, "bash"));
                    }
                }
                _ => {}
            }
        }
        None
    }

    /// Decide the command with the applicable bash rules in `rules`, each
    /// segment taking the first rule that matches it.
    fn bash_pool(&self, rules: &[Rule]) -> Option<RuleDecision> {
        let command = self.command?;
        if self.unparsed {
            // The floor found no deny; bash rules cannot vouch for segments
            // they cannot see.
            return Some(RuleDecision::Ask);
        }
        let pool: Vec<&BashRule> = rules
            .iter()
            .filter(|r| self.applies(r))
            .filter_map(|r| match r {
                Rule::Bash(b) => Some(b),
                _ => None,
            })
            .collect();
        let (mut any, mut all_allowed, mut ask) = (false, true, false);
        let segments = shell::split_composite(command)
            .into_iter()
            .map(shell::trim_blanks);
        for segment in segments.filter(|s| !s.is_empty()) {
            any = true;
            let first = pool.iter().find(|r| {
                r.patterns
                    .iter()
                    .any(|p| shell::pattern_matches(p, segment))
            });
            match first.map(|r| r.decision) {
                Some(RuleDecision::Deny) => return Some(RuleDecision::Deny),
                Some(RuleDecision::Ask) => ask = true,
                Some(RuleDecision::Allow) => {}
                None => all_allowed = false,
            }
        }
        if ask {
            Some(RuleDecision::Ask)
        } else {
            (any && all_allowed).then_some(RuleDecision::Allow)
        }
    }

    fn tool_matches(&self, rule: &ToolRule) -> bool {
        let name = self.call.tool_name;
        let named = rule.tool == "*"
            || rule.tool == name
            || mcp_server_prefix(&rule.tool).is_some_and(|prefix| name.starts_with(prefix));
        named
            && rule.skill.as_deref().is_none_or(|skill| {
                let loaded = self.call.input.get("skill").and_then(JsonValue::as_str);
                name == "Skill"
                    && (skill == "*"
                        || loaded.map(|s| s.strip_prefix('/').unwrap_or(s)) == Some(skill))
            })
    }

    /// The call's normalized path target and the root's segments, when the
    /// tool has a path field and the input names (or defaults to) a path.
    fn target(&self) -> Option<(Vec<&'a str>, Vec<&'a str>)> {
        let call = &self.call;
        let spec = path::field(self.policy, call.tool_name)?;
        let given = call
            .input
            .get(spec.field)
            .and_then(JsonValue::as_str)
            .filter(|s| !s.trim().is_empty());
        let raw = match given {
            Some(raw) if spec.virtual_root => raw.trim_start_matches('/'),
            Some(raw) => raw,
            None if spec.default_to_cwd => "",
            None => return None,
        };
        let root_text = self.policy.project_root.as_str();
        let mut root = Vec::new();
        path::push_segments(&mut root, root_text);
        Some((path::resolve(root_text, call.cwd, raw), root))
    }

    fn path_matches(&self, rule: &PathRule) -> bool {
        let call = &self.call;
        let covered = match &rule.tools {
            Some(tools) => tools.iter().any(|t| t == call.tool_name),
            None => call.scope == ToolScope::Write,
        };
        if !covered {
            return false;
        }
        let Some((target, root)) = self.target() else {
            return false;
        };
        if rule.decision == RuleDecision::Allow && !target.starts_with(&root) {
            return false;
        }
        rule.paths
            .iter()
            .any(|p| path::pattern_matches(p, &target, &root))
    }

    fn mode_default(&self) -> PermissionDecision {
        let scope = self.call.scope;
        let tool = self.call.tool_name;
        let reason = match self.policy.mode {
            _ if scope == ToolScope::Read || self.policy.auto_allow.contains(tool) => None,
            PermissionMode::SkipDangerous => None,
            PermissionMode::AcceptEdits if scope == ToolScope::Write => {
                let escapes = self
                    .target()
                    .is_some_and(|(target, root)| !target.starts_with(&root));
                escapes.then(|| format!("{tool} writes outside the project root"))
            }
            PermissionMode::AcceptEdits => {
                Some(format!("{tool} needs approval in accept_edits mode"))
            }
            PermissionMode::Default => Some(format!("{tool} needs approval in default mode")),
        };
        match reason {
            None => PermissionDecision::Allow {},
            Some(reason) => PermissionDecision::Ask {
                reason: Some(reason),
            },
        }
    }
}

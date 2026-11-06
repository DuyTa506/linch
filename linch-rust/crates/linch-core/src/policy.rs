//! Policy vocabulary shared by permissions, hooks and the scheduler.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Who a run acts for. Permission rules can match on its roles.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
pub struct Principal {
    tenant: String,
    user: String,
    roles: BTreeSet<String>,
}

/// A [`Principal`] field was empty or blank.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("principal {field} must be a non-blank string")]
pub struct PrincipalError {
    /// The offending field: `tenant`, `user` or `role`.
    pub field: &'static str,
}

impl Principal {
    /// A principal with non-blank tenant, user and role names.
    pub fn new(
        tenant: impl Into<String>,
        user: impl Into<String>,
        roles: impl IntoIterator<Item = impl Into<String>>,
    ) -> Result<Self, PrincipalError> {
        let tenant = non_blank(tenant.into(), "tenant")?;
        let user = non_blank(user.into(), "user")?;
        let roles = roles
            .into_iter()
            .map(|role| non_blank(role.into(), "role"))
            .collect::<Result<_, _>>()?;
        Ok(Self {
            tenant,
            user,
            roles,
        })
    }

    /// Tenant identifier.
    pub fn tenant(&self) -> &str {
        &self.tenant
    }

    /// User identifier.
    pub fn user(&self) -> &str {
        &self.user
    }

    /// Granted role names.
    pub fn roles(&self) -> &BTreeSet<String> {
        &self.roles
    }

    /// Whether the principal holds `role`.
    pub fn has_role(&self, role: &str) -> bool {
        self.roles.contains(role)
    }
}

impl<'de> Deserialize<'de> for Principal {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Raw {
            tenant: String,
            user: String,
            #[serde(default)]
            roles: Vec<String>,
        }
        let raw = Raw::deserialize(deserializer)?;
        Self::new(raw.tenant, raw.user, raw.roles).map_err(serde::de::Error::custom)
    }
}

fn non_blank(value: String, field: &'static str) -> Result<String, PrincipalError> {
    if value.trim().is_empty() {
        Err(PrincipalError { field })
    } else {
        Ok(value)
    }
}

/// Policy outcome for one tool call.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum PermissionDecision {
    /// Run the call.
    Allow {},
    /// Do not run the call; the reason is shown to the model.
    Deny {
        /// Why the call was denied.
        reason: String,
    },
    /// Suspend the run until a host approves or denies the call.
    Ask {
        /// Why approval is needed, for the approval prompt.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        reason: Option<String>,
    },
}

/// A named point where a host callback can intercept a run (tier 2).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HookPoint {
    /// A user prompt is about to enter the session.
    UserPromptSubmit,
    /// The provider request is being assembled.
    ContextBuild,
    /// A validated tool call is about to be checked and run.
    PreToolUse,
    /// A tool call needs approval.
    PermissionRequest,
    /// A tool call finished.
    PostToolUse,
    /// The model produced a text-only response; the run may stop.
    Stop,
    /// A turn finished.
    PostTurn,
}

/// What happens when a callback errors or times out.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailMode {
    /// Treat the failure as a block (deny the call or stop the action).
    Closed,
    /// Ignore the failed callback and continue.
    Open,
}

impl HookPoint {
    /// Default failure handling: tool gates fail closed, the rest fail open,
    /// so a dead context source cannot stall every turn.
    pub fn default_fail_mode(self) -> FailMode {
        match self {
            Self::PreToolUse | Self::PermissionRequest => FailMode::Closed,
            Self::UserPromptSubmit
            | Self::ContextBuild
            | Self::PostToolUse
            | Self::Stop
            | Self::PostTurn => FailMode::Open,
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn principal_rejects_blank_fields() {
        assert_eq!(Principal::new(" ", "u", ["r"]).unwrap_err().field, "tenant");
        assert_eq!(Principal::new("t", "", ["r"]).unwrap_err().field, "user");
        assert_eq!(
            Principal::new("t", "u", ["ok", "\t"]).unwrap_err().field,
            "role"
        );
        let p = Principal::new("t", "u", ["admin"]).unwrap();
        assert!(p.has_role("admin") && !p.has_role("dev"));
    }

    #[test]
    fn principal_decode_validates() {
        let ok: Principal =
            serde_json::from_value(json!({"tenant": "t", "user": "u", "roles": ["a"]})).unwrap();
        assert_eq!(ok.tenant(), "t");
        assert!(serde_json::from_value::<Principal>(json!({"tenant": "", "user": "u"})).is_err());
        assert!(
            serde_json::from_value::<Principal>(json!({"tenant": "t", "user": "u", "x": 1}))
                .is_err()
        );
    }

    #[test]
    fn permission_decision_wire_shape() {
        assert_eq!(
            serde_json::to_value(PermissionDecision::Allow {}).unwrap(),
            json!({"type": "allow"})
        );
        let deny = PermissionDecision::Deny {
            reason: "no".into(),
        };
        let wire = serde_json::to_value(&deny).unwrap();
        assert_eq!(wire, json!({"type": "deny", "reason": "no"}));
        assert_eq!(
            serde_json::from_value::<PermissionDecision>(wire).unwrap(),
            deny
        );
        assert!(
            serde_json::from_value::<PermissionDecision>(json!({"type": "allow", "x": 1})).is_err()
        );
    }

    #[test]
    fn tool_gates_fail_closed_by_default() {
        assert_eq!(HookPoint::PreToolUse.default_fail_mode(), FailMode::Closed);
        assert_eq!(
            HookPoint::PermissionRequest.default_fail_mode(),
            FailMode::Closed
        );
        assert_eq!(HookPoint::ContextBuild.default_fail_mode(), FailMode::Open);
        assert_eq!(HookPoint::Stop.default_fail_mode(), FailMode::Open);
    }
}

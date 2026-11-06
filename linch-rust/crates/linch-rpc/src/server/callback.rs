//! Intercept callbacks (tier 2): one W-C [`Hook`] per declaration that
//! sends `hook.call` to the client. The chain evaluates the matcher, the
//! timeout and the fail mode in Rust, so a filtered-out occurrence never
//! crosses the connection.

use std::marker::PhantomData;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use linch::hooks::{
    ApprovalRequest, ContextBuild, ContextBuildRequest, ContextBuildResponse, PermissionRequest,
    PostToolUse, PostToolUseRequest, PostToolUseResponse, PostTurn, PostTurnRequest, PreToolUse,
    PreToolUseRequest, PreToolUseResponse, Stop, StopRequest, StopResponse, UserPromptRequest,
    UserPromptResponse, UserPromptSubmit,
};
use linch::{
    CallbackError, FailMode, Hook, HookChain, HookError, HookPoint, Hooks, JsonValue, Point,
    ToolMatcher,
};
use serde::de::DeserializeOwned;
use serde_json::json;

use super::peer::Peer;
use crate::error::{ErrorObject, invalid_params};
use crate::protocol::{
    CallbackSpec, ContextBuildAnswer, Decision, HookCallParams, PostToolUseAnswer, PostTurnAnswer,
    PreToolUseAnswer, StopAnswer, UserPromptAnswer, WireToolResult, method,
};

/// Wait for an answer when the declaration sets no `timeout_ms`.
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(30);

/// The hook chains holding one [`RpcHook`] per declaration, in order.
///
/// Raises:
///     ErrorObject: Invalid params for a blank policy id, an empty matcher,
///         a matcher at a point that is not about a tool, or `closed` at
///         `context_build` or `post_turn`.
pub(super) fn hooks(
    specs: Vec<CallbackSpec>,
    agent: &str,
    peer: &Peer,
) -> Result<Arc<Hooks>, ErrorObject> {
    let hooks = Hooks::default();
    for spec in specs {
        register(&hooks, spec, agent, peer)
            .map_err(|error| invalid_params(&format!("callbacks: {error}")))?;
    }
    Ok(Arc::new(hooks))
}

fn register(hooks: &Hooks, spec: CallbackSpec, agent: &str, peer: &Peer) -> Result<(), HookError> {
    let matcher = spec.matcher.map(ToolMatcher::new).transpose()?;
    let common = Common {
        agent: agent.to_owned(),
        policy_id: spec.policy_id,
        policy_version: spec.policy_version,
        matcher,
        timeout: spec
            .timeout_ms
            .map_or(DEFAULT_TIMEOUT, |ms| Duration::from_millis(ms.get())),
        fail_mode: spec.fail_mode,
        peer: peer.clone(),
    };
    match spec.point {
        HookPoint::UserPromptSubmit => add(&hooks.user_prompt_submit, common),
        HookPoint::ContextBuild => add(&hooks.context_build, common),
        HookPoint::PreToolUse => add(&hooks.pre_tool_use, common),
        HookPoint::PermissionRequest => add(&hooks.permission_request, common),
        HookPoint::PostToolUse => add(&hooks.post_tool_use, common),
        HookPoint::Stop => add(&hooks.stop, common),
        HookPoint::PostTurn => add(&hooks.post_turn, common),
    }
}

fn add<P: Wire>(chain: &HookChain<P>, common: Common) -> Result<(), HookError> {
    let hook = RpcHook::<P> {
        common,
        point: PhantomData,
    };
    // The chains live as long as the agent; the handle is not needed.
    let _ = chain.register(Arc::new(hook))?;
    Ok(())
}

/// What every callback has, whatever its point.
struct Common {
    agent: String,
    policy_id: String,
    policy_version: u32,
    matcher: Option<ToolMatcher>,
    timeout: Duration,
    fail_mode: Option<FailMode>,
    peer: Peer,
}

/// A callback at point `P`, answered by the client.
struct RpcHook<P> {
    common: Common,
    point: PhantomData<fn() -> P>,
}

#[async_trait]
impl<P: Wire> Hook<P> for RpcHook<P> {
    fn policy_id(&self) -> &str {
        &self.common.policy_id
    }

    fn policy_version(&self) -> u32 {
        self.common.policy_version
    }

    fn matcher(&self) -> Option<ToolMatcher> {
        self.common.matcher.clone()
    }

    /// The chain applies it and drops the call on expiry, which sends
    /// `$/cancelRequest`.
    fn timeout(&self) -> Option<Duration> {
        Some(self.common.timeout)
    }

    fn fail_mode(&self) -> Option<FailMode> {
        self.common.fail_mode
    }

    async fn call(&self, request: &P::Request) -> Result<P::Response, CallbackError> {
        let params = HookCallParams {
            agent: self.common.agent.clone(),
            point: P::POINT,
            policy_id: self.common.policy_id.clone(),
            request: P::encode(request),
        };
        let params = match serde_json::to_value(params) {
            Ok(JsonValue::Object(params)) => params,
            other => unreachable!("hook.call params encode as an object, got {other:?}"),
        };
        let peer = &self.common.peer;
        let answer = peer.call(method::HOOK_CALL, params, None, None).await?;
        P::decode(answer).map_err(|error| format!("malformed hook answer: {error}").into())
    }
}

/// A point's request and answer on the wire.
trait Wire: Point {
    /// The occurrence, as `hook.call`'s `request` member.
    fn encode(request: &Self::Request) -> JsonValue;

    /// The client's answer, as the point's response.
    fn decode(answer: JsonValue) -> Result<Self::Response, serde_json::Error>;
}

fn answer<T: DeserializeOwned>(answer: JsonValue) -> Result<T, serde_json::Error> {
    serde_json::from_value(answer)
}

impl Wire for PreToolUse {
    fn encode(r: &PreToolUseRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "tool_use_id": r.tool_use_id,
               "tool_name": r.tool_name, "input": r.input})
    }

    fn decode(value: JsonValue) -> Result<PreToolUseResponse, serde_json::Error> {
        Ok(match answer(value)? {
            PreToolUseAnswer::Continue {} => PreToolUseResponse::Continue,
            PreToolUseAnswer::Rewrite { input } => PreToolUseResponse::Rewrite { input },
            PreToolUseAnswer::Block { reason } => PreToolUseResponse::Block { reason },
            PreToolUseAnswer::Resolve { result } => PreToolUseResponse::Resolve {
                result: result.into(),
            },
        })
    }
}

impl Wire for PermissionRequest {
    fn encode(r: &ApprovalRequest) -> JsonValue {
        let mut fields = json!({"session_id": r.session_id, "run_id": r.run_id,
                                "tool_use_id": r.tool_use_id, "tool_name": r.tool_name,
                                "input": r.input});
        if let Some(reason) = &r.reason {
            fields["reason"] = json!(reason);
        }
        if let Some(principal) = &r.principal {
            fields["principal"] = json!(principal);
        }
        fields
    }

    fn decode(value: JsonValue) -> Result<linch::hooks::ApprovalResponse, serde_json::Error> {
        answer::<Decision>(value).map(Into::into)
    }
}

impl Wire for PostToolUse {
    fn encode(r: &PostToolUseRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "tool_use_id": r.tool_use_id,
               "tool_name": r.tool_name, "input": r.input,
               "result": WireToolResult::from(&r.result)})
    }

    fn decode(value: JsonValue) -> Result<PostToolUseResponse, serde_json::Error> {
        Ok(match answer(value)? {
            PostToolUseAnswer::Continue {} => PostToolUseResponse::Continue,
            PostToolUseAnswer::Replace { result } => PostToolUseResponse::Replace {
                result: result.into(),
            },
        })
    }
}

impl Wire for UserPromptSubmit {
    fn encode(r: &UserPromptRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "content": r.content})
    }

    fn decode(value: JsonValue) -> Result<UserPromptResponse, serde_json::Error> {
        Ok(match answer(value)? {
            UserPromptAnswer::Continue {} => UserPromptResponse::Continue,
            UserPromptAnswer::Rewrite { content } => UserPromptResponse::Rewrite { content },
            UserPromptAnswer::Block { reason } => UserPromptResponse::Block { reason },
        })
    }
}

impl Wire for ContextBuild {
    fn encode(r: &ContextBuildRequest) -> JsonValue {
        let messages: Vec<&linch::Message> = r.messages.iter().map(|m| &**m).collect();
        json!({"session_id": r.session_id, "run_id": r.run_id, "messages": messages})
    }

    fn decode(value: JsonValue) -> Result<ContextBuildResponse, serde_json::Error> {
        let ContextBuildAnswer { blocks } = answer(value)?;
        Ok(ContextBuildResponse { blocks })
    }
}

impl Wire for Stop {
    fn encode(r: &StopRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "text": r.text})
    }

    fn decode(value: JsonValue) -> Result<StopResponse, serde_json::Error> {
        Ok(match answer(value)? {
            StopAnswer::Allow {} => StopResponse::Allow,
            StopAnswer::Continue { feedback } => StopResponse::Continue { feedback },
        })
    }
}

impl Wire for PostTurn {
    fn encode(r: &PostTurnRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "turn": r.turn,
               "stop_reason": r.stop_reason})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        answer::<PostTurnAnswer>(value).map(|_| ())
    }
}

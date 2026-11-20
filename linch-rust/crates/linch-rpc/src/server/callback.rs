//! Intercept callbacks (tier 2): one W-C [`Hook`] per declaration that
//! sends `hook.call` to the client. The chain evaluates the matcher, the
//! timeout and the fail mode in Rust, so a filtered-out occurrence never
//! crosses the connection.

use std::marker::PhantomData;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use async_trait::async_trait;
use linch::hooks::{
    AfterProviderCall, AfterProviderCallRequest, AfterProviderCallResponse, AgentStart,
    AgentStartRequest, AgentStop, AgentStopRequest, ApprovalRequest, BeforeProviderCall,
    BeforeProviderCallRequest, BeforeProviderCallResponse, ContextBuild, ContextBuildRequest,
    ContextBuildResponse, EventEmit, EventEmitRequest, Next, PermissionRequest, PostCompact,
    PostCompactRequest, PostToolUse, PostToolUseFailure, PostToolUseFailureRequest,
    PostToolUseRequest, PostToolUseResponse, PostTurn, PostTurnRequest, PreCompact,
    PreCompactRequest, PreToolUse, PreToolUseRequest, PreToolUseResponse, ProviderCallStart,
    ProviderCallStartRequest, ProviderCallStop, ProviderCallStopRequest, Stop, StopRequest,
    StopResponse, SubagentStart, SubagentStartRequest, SubagentStop, SubagentStopRequest, ToolCall,
    ToolMiddleware, ToolUseStart, ToolUseStartRequest, ToolUseStop, ToolUseStopRequest, TurnStart,
    TurnStartRequest, UserPromptRequest, UserPromptResponse, UserPromptSubmit,
};
use linch::{
    CallbackError, FailMode, Hook, HookChain, HookError, HookPoint, Hooks, JsonValue, Point,
    ToolMatcher, ToolOutputError, ToolResult,
};
use serde::de::DeserializeOwned;
use serde_json::json;

use super::peer::Peer;
use crate::access::Tenant;
use crate::error::{ErrorObject, invalid_params};
use crate::protocol::{
    AfterProviderCallAnswer, BeforeProviderCallAnswer, CallbackSpec, ContextBuildAnswer,
    HookCallParams, ObserveAnswer, PostToolUseAnswer, PostTurnAnswer, PreToolUseAnswer, StopAnswer,
    ToolExecuteAnswer, UserPromptAnswer, WireToolResult, method,
};

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
    tenant: Option<&Arc<Tenant>>,
) -> Result<Arc<Hooks>, ErrorObject> {
    let hooks = Hooks::default();
    for spec in specs {
        register(&hooks, spec, agent, peer, tenant)
            .map_err(|error| invalid_params(&format!("callbacks: {error}")))?;
    }
    Ok(Arc::new(hooks))
}

fn register(
    hooks: &Hooks,
    spec: CallbackSpec,
    agent: &str,
    peer: &Peer,
    tenant: Option<&Arc<Tenant>>,
) -> Result<(), HookError> {
    let matcher = spec.matcher.map(ToolMatcher::new).transpose()?;
    let common = Common {
        agent: agent.to_owned(),
        policy_id: spec.policy_id,
        policy_version: spec.policy_version,
        matcher,
        // Absent: no limit, as in Python. The client's answer, `run.cancel`
        // or the end of the connection end the wait.
        timeout: spec.timeout_ms.map(|ms| Duration::from_millis(ms.get())),
        fail_mode: spec.fail_mode,
        peer: peer.clone(),
        tenant: tenant.cloned(),
    };
    match spec.point {
        HookPoint::UserPromptSubmit => add(&hooks.user_prompt_submit, common),
        HookPoint::ContextBuild => add(&hooks.context_build, common),
        HookPoint::PreToolUse => add(&hooks.pre_tool_use, common),
        HookPoint::PermissionRequest => add(&hooks.permission_request, common),
        HookPoint::PostToolUse => add(&hooks.post_tool_use, common),
        HookPoint::Stop => add(&hooks.stop, common),
        HookPoint::PostTurn => add(&hooks.post_turn, common),
        HookPoint::AgentStart => add(&hooks.agent_start, common),
        HookPoint::AgentStop => add(&hooks.agent_stop, common),
        HookPoint::TurnStart => add(&hooks.turn_start, common),
        HookPoint::BeforeProviderCall => add(&hooks.before_provider_call, common),
        HookPoint::ProviderCallStart => add(&hooks.provider_call_start, common),
        HookPoint::ProviderCallStop => add(&hooks.provider_call_stop, common),
        HookPoint::AfterProviderCall => add(&hooks.after_provider_call, common),
        HookPoint::ToolUseStart => add(&hooks.tool_use_start, common),
        HookPoint::ToolUseStop => add(&hooks.tool_use_stop, common),
        HookPoint::PostToolUseFailure => add(&hooks.post_tool_use_failure, common),
        HookPoint::PreCompact => add(&hooks.pre_compact, common),
        HookPoint::PostCompact => add(&hooks.post_compact, common),
        HookPoint::SubagentStart => add(&hooks.subagent_start, common),
        HookPoint::SubagentStop => add(&hooks.subagent_stop, common),
        HookPoint::EventEmit => add(&hooks.event_emit, common),
        HookPoint::ToolExecute => {
            let middleware = RpcMiddleware {
                common,
                tokens: AtomicU64::new(0),
            };
            // The pipeline lives as long as the agent; the handle is not needed.
            let _ = hooks.tool_execute.register(Arc::new(middleware))?;
            Ok(())
        }
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
    timeout: Option<Duration>,
    fail_mode: Option<FailMode>,
    peer: Peer,
    /// The tenant the agent belongs to, whose allowlists bound what an
    /// answer may change; `None` on a local connection.
    tenant: Option<Arc<Tenant>>,
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
        self.common.timeout
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
        let answer =
            P::decode(answer).map_err(|error| format!("malformed hook answer: {error}"))?;
        P::confine(&self.common, request, answer).map_err(Into::into)
    }
}

/// A `tool_execute` callback: a layer of the tool waterfall whose
/// `next` is the client's answer. `before` asks whether to run what is
/// inside; with `after`, a second call hands over the result.
struct RpcMiddleware {
    common: Common,
    /// Pairs a `before` call with its `after`: one token per attempt.
    tokens: AtomicU64,
}

impl RpcMiddleware {
    fn fail_mode(&self) -> FailMode {
        (self.common.fail_mode).unwrap_or_else(|| HookPoint::ToolExecute.default_fail_mode())
    }

    /// One `hook.call` of the point, with its timeout, decoded.
    async fn ask(
        &self,
        call: &ToolCall,
        phase: &str,
        token: u64,
        result: Option<&ToolResult>,
    ) -> Result<ToolExecuteAnswer, CallbackError> {
        let mut request = json!({"session_id": call.session_id, "run_id": call.run_id,
            "tool_use_id": call.tool_use_id, "tool_name": call.tool_name,
            "input": call.input, "attempt": call.attempt, "phase": phase, "token": token});
        if let Some(result) = result {
            request["result"] = json!(WireToolResult::from(result));
        }
        let params = HookCallParams {
            agent: self.common.agent.clone(),
            point: HookPoint::ToolExecute,
            policy_id: self.common.policy_id.clone(),
            request,
        };
        let params = match serde_json::to_value(params) {
            Ok(JsonValue::Object(params)) => params,
            other => unreachable!("hook.call params encode as an object, got {other:?}"),
        };
        let asking = self.common.peer.call(method::HOOK_CALL, params, None, None);
        let answer = match self.common.timeout {
            Some(limit) => tokio::time::timeout(limit, asking)
                .await
                .map_err(|_| "timed out")??,
            None => asking.await?,
        };
        serde_json::from_value(answer)
            .map_err(|error| format!("malformed hook answer: {error}").into())
    }

    /// The result a failed callback leaves: `None` to go on (fail open).
    fn failed(&self, what: &str) -> Option<ToolResult> {
        match self.fail_mode() {
            FailMode::Open => None,
            FailMode::Closed => Some(Err(ToolOutputError::new(format!(
                "hook `{}` {what}",
                self.common.policy_id
            )))),
        }
    }
}

#[async_trait]
impl ToolMiddleware for RpcMiddleware {
    fn policy_id(&self) -> &str {
        &self.common.policy_id
    }

    fn policy_version(&self) -> u32 {
        self.common.policy_version
    }

    fn matcher(&self) -> Option<ToolMatcher> {
        self.common.matcher.clone()
    }

    async fn call(&self, call: &ToolCall, next: Next<'_>) -> ToolResult {
        let token = self.tokens.fetch_add(1, Ordering::Relaxed);
        let after = match self.ask(call, "before", token, None).await {
            Ok(ToolExecuteAnswer::Next { after }) => after,
            Ok(ToolExecuteAnswer::Veto { result }) => return result.into(),
            Ok(_) | Err(_) => match self.failed("failed") {
                Some(refused) => return refused,
                None => false,
            },
        };
        let result = next.run().await;
        if !after {
            return result;
        }
        match self.ask(call, "after", token, Some(&result)).await {
            Ok(ToolExecuteAnswer::Continue {}) => result,
            Ok(ToolExecuteAnswer::Replace { result }) => result.into(),
            // A closed failure withholds the result, as at `post_tool_use`.
            Ok(_) | Err(_) => match self.failed("failed") {
                Some(_) => Err(ToolOutputError::new(format!(
                    "hook `{}` failed; the tool result was withheld",
                    self.common.policy_id
                ))),
                None => result,
            },
        }
    }
}

/// A point's request and answer on the wire.
trait Wire: Point {
    /// The occurrence, as `hook.call`'s `request` member.
    fn encode(request: &Self::Request) -> JsonValue;

    /// The client's answer, as the point's response.
    fn decode(answer: JsonValue) -> Result<Self::Response, serde_json::Error>;

    /// The answer as the tenant's allowlists let it stand: what it may not
    /// change is put back, or the answer is refused. The default changes
    /// nothing.
    fn confine(
        _common: &Common,
        _request: &Self::Request,
        answer: Self::Response,
    ) -> Result<Self::Response, String> {
        Ok(answer)
    }
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
        answer(value)
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
            PostToolUseAnswer::Mutate { content, is_error } => {
                PostToolUseResponse::Mutate { content, is_error }
            }
        })
    }
}

impl Wire for UserPromptSubmit {
    fn encode(r: &UserPromptRequest) -> JsonValue {
        let mut request =
            json!({"session_id": r.session_id, "run_id": r.run_id, "content": r.content});
        // Only a steer names its source; a new run's prompt stays as it was.
        if let (Some(source), Some(object)) = (r.source, request.as_object_mut()) {
            object.insert("source".into(), json!(source));
        }
        request
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
        let ContextBuildAnswer { blocks, metadata } = answer(value)?;
        Ok(ContextBuildResponse { blocks, metadata })
    }
}

impl Wire for Stop {
    fn encode(r: &StopRequest) -> JsonValue {
        let mut request = json!({"session_id": r.session_id, "run_id": r.run_id, "text": r.text});
        // Only a run with an output schema sends the key.
        if let Some(value) = &r.structured_output {
            request["structured_output"] = value.clone().into();
        }
        request
    }

    fn decode(value: JsonValue) -> Result<StopResponse, serde_json::Error> {
        Ok(match answer(value)? {
            StopAnswer::Allow {} => StopResponse::Allow,
            StopAnswer::Continue { feedback } | StopAnswer::ForceContinue { feedback } => {
                StopResponse::Continue { feedback }
            }
            StopAnswer::Retry { feedback } => StopResponse::Retry { feedback },
            StopAnswer::Rewrite { text } => StopResponse::Rewrite { text },
            StopAnswer::Block { reason } => StopResponse::Block { reason },
        })
    }
}

impl Wire for BeforeProviderCall {
    fn encode(r: &BeforeProviderCallRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "turn": r.turn,
               "request": r.request})
    }

    fn decode(value: JsonValue) -> Result<BeforeProviderCallResponse, serde_json::Error> {
        Ok(match answer(value)? {
            BeforeProviderCallAnswer::Continue {} => BeforeProviderCallResponse::Continue,
            BeforeProviderCallAnswer::Rewrite { request } => {
                BeforeProviderCallResponse::Rewrite { request }
            }
            BeforeProviderCallAnswer::ForceContinue { feedback } => {
                BeforeProviderCallResponse::ForceContinue { feedback }
            }
            BeforeProviderCallAnswer::Block { reason } => {
                BeforeProviderCallResponse::Block { reason }
            }
        })
    }

    /// A tenant's rewrite keeps the operator's `system_sections` and the
    /// system bound of `agent.open`.
    fn confine(
        common: &Common,
        request: &BeforeProviderCallRequest,
        answer: BeforeProviderCallResponse,
    ) -> Result<BeforeProviderCallResponse, String> {
        match (&common.tenant, answer) {
            (Some(tenant), BeforeProviderCallResponse::Rewrite { request: rewritten }) => {
                let confined = tenant.confine_rewrite(&request.request, *rewritten)?;
                Ok(BeforeProviderCallResponse::Rewrite {
                    request: Box::new(confined),
                })
            }
            (_, answer) => Ok(answer),
        }
    }
}

impl Wire for AfterProviderCall {
    fn encode(r: &AfterProviderCallRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "turn": r.turn,
               "assembly": r.assembly})
    }

    fn decode(value: JsonValue) -> Result<AfterProviderCallResponse, serde_json::Error> {
        Ok(match answer(value)? {
            AfterProviderCallAnswer::Continue {} => AfterProviderCallResponse::Continue,
            AfterProviderCallAnswer::Rewrite { assembly } => {
                AfterProviderCallResponse::Rewrite { assembly }
            }
            AfterProviderCallAnswer::Retry { feedback }
            | AfterProviderCallAnswer::ForceContinue { feedback } => {
                AfterProviderCallResponse::Retry { feedback }
            }
            AfterProviderCallAnswer::Block { reason } => {
                AfterProviderCallResponse::Block { reason }
            }
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

/// Decode an observe-only point's `{}` answer. Every point below shares it:
/// none can fail closed, so there is nothing for the client to decide.
fn observed(value: JsonValue) -> Result<(), serde_json::Error> {
    answer::<ObserveAnswer>(value).map(|_| ())
}

impl Wire for AgentStart {
    fn encode(r: &AgentStartRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "model": r.model,
               "prompt": r.prompt, "tools": r.tools})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

impl Wire for AgentStop {
    fn encode(r: &AgentStopRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "status": r.status,
               "stop_reason": r.stop_reason})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

impl Wire for TurnStart {
    fn encode(r: &TurnStartRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "turn": r.turn})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

impl Wire for ProviderCallStart {
    fn encode(r: &ProviderCallStartRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "turn": r.turn,
               "model": r.model})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

impl Wire for ProviderCallStop {
    fn encode(r: &ProviderCallStopRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "turn": r.turn,
               "model": r.model, "disposition": r.disposition,
               "usage": r.usage, "duration_ms": r.duration_ms})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

impl Wire for ToolUseStart {
    fn encode(r: &ToolUseStartRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id,
               "tool_use_id": r.tool_use_id, "tool_name": r.tool_name, "input": r.input})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

impl Wire for ToolUseStop {
    fn encode(r: &ToolUseStopRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id,
               "tool_use_id": r.tool_use_id, "tool_name": r.tool_name,
               "is_error": r.is_error, "duration_ms": r.duration_ms})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

impl Wire for PostToolUseFailure {
    fn encode(r: &PostToolUseFailureRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id,
               "tool_use_id": r.tool_use_id, "tool_name": r.tool_name, "input": r.input,
               "error": r.error})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

impl Wire for PreCompact {
    fn encode(r: &PreCompactRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "turn": r.turn,
               "messages": r.messages, "tokens": r.tokens, "strategy": r.strategy})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

impl Wire for PostCompact {
    fn encode(r: &PostCompactRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "turn": r.turn,
               "messages_before": r.messages_before, "messages_after": r.messages_after,
               "tokens_before": r.tokens_before, "tokens_after": r.tokens_after,
               "strategy": r.strategy})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

impl Wire for SubagentStart {
    fn encode(r: &SubagentStartRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id,
               "child_session_id": r.child_session_id, "subagent_run_id": r.subagent_run_id,
               "subagent_type": r.subagent_type, "display_name": r.display_name,
               "prompt": r.prompt})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

impl Wire for SubagentStop {
    fn encode(r: &SubagentStopRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id,
               "child_session_id": r.child_session_id, "subagent_run_id": r.subagent_run_id,
               "subagent_type": r.subagent_type, "display_name": r.display_name,
               "subtype": r.subtype, "text": r.text, "error": r.error})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

impl Wire for EventEmit {
    fn encode(r: &EventEmitRequest) -> JsonValue {
        json!({"session_id": r.session_id, "run_id": r.run_id, "event": r.event})
    }

    fn decode(value: JsonValue) -> Result<(), serde_json::Error> {
        observed(value)
    }
}

#[cfg(test)]
mod tests;

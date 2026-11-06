//! Host tools (tier 2'): tools declared in `agent.open` and run by the
//! client through `tool.call`.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use linch::{
    JsonObject, JsonValue, Tool, ToolContext, ToolOutput, ToolOutputError, ToolRegistry,
    ToolResult, ToolScope,
};

use super::peer::{CallError, Peer};
use crate::error::{ErrorObject, invalid_params};
use crate::protocol::{HostToolSpec, ToolCallParams, method, tool_code};

/// Wait for one call when the declaration sets no `timeout_ms`.
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(300);

/// One host tool: every call crosses the connection that declared it.
struct HostTool {
    spec: HostToolSpec,
    agent: String,
    peer: Peer,
}

/// A registry holding one [`HostTool`] per declaration, in order.
///
/// Raises:
///     ErrorObject: Invalid params when a name is invalid or taken twice, or
///         a schema is not a valid object schema.
pub(super) fn registry(
    specs: Vec<HostToolSpec>,
    agent: &str,
    peer: &Peer,
) -> Result<Arc<ToolRegistry>, ErrorObject> {
    let tools = Arc::new(ToolRegistry::new());
    for spec in specs {
        let tool = HostTool {
            spec,
            agent: agent.to_owned(),
            peer: peer.clone(),
        };
        // The registry lives as long as the agent; the handle is not needed.
        let _ = tools
            .register(Arc::new(tool))
            .map_err(|error| invalid_params(&format!("tools: {error}")))?;
    }
    Ok(tools)
}

#[async_trait]
impl Tool for HostTool {
    fn name(&self) -> &str {
        &self.spec.name
    }

    fn description(&self) -> &str {
        &self.spec.description
    }

    fn input_schema(&self) -> &JsonObject {
        &self.spec.input_schema
    }

    fn scope(&self) -> ToolScope {
        self.spec.scope
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let params = ToolCallParams {
            agent: self.agent.clone(),
            name: self.spec.name.clone(),
            input,
            session_id: ctx.session_id.clone(),
            run_id: ctx.run_id.clone(),
            tool_use_id: ctx.tool_use_id.clone(),
            principal: ctx.principal.clone(),
        };
        let params = match serde_json::to_value(params) {
            Ok(JsonValue::Object(params)) => params,
            other => unreachable!("tool.call params encode as an object, got {other:?}"),
        };
        let limit = self
            .spec
            .timeout_ms
            .map_or(DEFAULT_TIMEOUT, |ms| Duration::from_millis(ms.get()));
        let answer = self
            .peer
            .call(method::TOOL_CALL, params, Some(limit), Some(&ctx.cancel));
        match answer.await {
            Ok(value) => serde_json::from_value::<ToolOutput>(value).map_err(|error| {
                let message = format!(
                    "Host tool {:?} returned a malformed result: {error}",
                    self.spec.name
                );
                failure(tool_code::PROTOCOL_ERROR, message)
            }),
            Err(CallError::Rpc(error)) => Err(ToolOutputError {
                message: error.message,
                code: Some(tool_code::TOOL_ERROR.into()),
                details: error.data,
            }),
            Err(CallError::Timeout(after)) => {
                let message = format!(
                    "Host tool {:?} timed out after {}ms",
                    self.spec.name,
                    after.as_millis()
                );
                Err(failure(tool_code::TIMEOUT, message))
            }
            Err(CallError::Cancelled) => Err(failure(tool_code::ABORTED, "Tool call aborted")),
            Err(CallError::Closed) => Err(failure(
                tool_code::UNAVAILABLE,
                "The host connection is closed",
            )),
        }
    }
}

fn failure(code: &str, message: impl Into<String>) -> ToolOutputError {
    ToolOutputError {
        message: message.into(),
        code: Some(code.into()),
        details: None,
    }
}

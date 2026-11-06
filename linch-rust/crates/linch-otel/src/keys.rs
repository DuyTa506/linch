//! Span names, attribute keys and metric names, the same strings Python
//! Linch uses (`src/linch/_telemetry.py`).
//!
//! The `gen_ai.*` keys follow the OpenTelemetry GenAI semantic conventions,
//! which are still experimental; keeping them here keeps a convention update
//! local. The `linch.*` keys are Linch's own.

/// `gen_ai.operation.name`: `invoke_agent`, `step`, `chat`, `compaction`
/// or `execute_tool`.
pub const OPERATION: &str = "gen_ai.operation.name";
/// `gen_ai.provider.name`: the provider, as named by [`crate::provider_name`].
pub const PROVIDER: &str = "gen_ai.provider.name";
/// `gen_ai.request.model`: the model the run asks for.
pub const MODEL: &str = "gen_ai.request.model";
/// `gen_ai.response.model`: the model that served a call. The event stream
/// does not carry it yet, so this crate never sets it.
pub const RESPONSE_MODEL: &str = "gen_ai.response.model";
/// `gen_ai.response.finish_reasons`: a one-element array with the
/// response's stop reason.
pub const FINISH_REASONS: &str = "gen_ai.response.finish_reasons";
/// `gen_ai.usage.input_tokens`: uncached input tokens.
pub const INPUT_TOKENS: &str = "gen_ai.usage.input_tokens";
/// `gen_ai.usage.output_tokens`: generated tokens.
pub const OUTPUT_TOKENS: &str = "gen_ai.usage.output_tokens";
/// `gen_ai.usage.cache_read.input_tokens`: input tokens read from the
/// prompt cache.
pub const CACHE_READ_TOKENS: &str = "gen_ai.usage.cache_read.input_tokens";
/// `gen_ai.usage.cache_creation.input_tokens`: input tokens written to the
/// prompt cache.
pub const CACHE_CREATION_TOKENS: &str = "gen_ai.usage.cache_creation.input_tokens";
/// `gen_ai.conversation.id`: the session id.
pub const CONVERSATION_ID: &str = "gen_ai.conversation.id";
/// `gen_ai.tool.name`: the tool a call ran.
pub const TOOL_NAME: &str = "gen_ai.tool.name";
/// `gen_ai.tool.call.id`: the provider's id for the tool call.
pub const TOOL_CALL_ID: &str = "gen_ai.tool.call.id";
/// `gen_ai.input.messages`: captured input, as JSON text.
pub const INPUT_MESSAGES: &str = "gen_ai.input.messages";
/// `gen_ai.output.messages`: captured output, as JSON text.
pub const OUTPUT_MESSAGES: &str = "gen_ai.output.messages";
/// `gen_ai.tool.call.arguments`: captured tool input, as JSON text.
pub const TOOL_ARGUMENTS: &str = "gen_ai.tool.call.arguments";
/// `gen_ai.tool.call.result`: captured tool result text.
pub const TOOL_RESULT: &str = "gen_ai.tool.call.result";
/// `gen_ai.token.type`: `input` or `output`, on the token metric.
pub const TOKEN_TYPE: &str = "gen_ai.token.type";
/// `linch.run_id`: the run.
pub const RUN_ID: &str = "linch.run_id";
/// `linch.turn_index`: the turn, from 0.
pub const TURN_INDEX: &str = "linch.turn_index";
/// `linch.resumed`: whether this call continues an existing run.
pub const RESUMED: &str = "linch.resumed";
/// `linch.stop_reason`: why the run's loop stopped.
pub const STOP_REASON: &str = "linch.stop_reason";
/// `linch.compaction.strategy`: the compaction strategy that ran.
pub const COMPACTION_STRATEGY: &str = "linch.compaction.strategy";
/// `linch.cost_usd`: the cost of a call, or of the run, in USD, when the
/// agent prices its model. Rust only: Python Linch does not export cost.
pub const COST_USD: &str = "linch.cost_usd";
/// `error.type`: the error's name only, never its message, which may hold
/// private content.
pub const ERROR_TYPE: &str = "error.type";

/// `gen_ai.client.token.usage`: a histogram of tokens per model call, unit
/// `{token}`, tagged with [`TOKEN_TYPE`].
pub const TOKEN_USAGE: &str = "gen_ai.client.token.usage";
/// `gen_ai.client.operation.duration`: a histogram of model call time,
/// unit `s`.
pub const OPERATION_DURATION: &str = "gen_ai.client.operation.duration";

/// The run span's name.
pub const INVOKE_AGENT: &str = "invoke_agent";
/// The per-turn span's name.
pub const STEP: &str = "step";
/// The model call operation; its span is named `chat {model}`.
pub const CHAT: &str = "chat";
/// The compaction span's name.
pub const COMPACTION: &str = "compaction";
/// The tool call operation; its span is named `execute_tool {tool}`.
pub const EXECUTE_TOOL: &str = "execute_tool";

/// Environment switch for content capture, read by
/// [`crate::capture_content_from_env`].
pub const CAPTURE_CONTENT_ENV: &str = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT";

//! [`Event::type_name`]: the wire `type` tag of an event, without encoding
//! it.

use super::Event;

impl Event {
    /// The event's wire `type` tag (`init`, `tool_call_end`, ...).
    ///
    /// Exhaustive by variant, so a new kind cannot be added without its
    /// name. A `subagent` event is `subagent` whatever it wraps, and an
    /// [`super::IgnorableEvent`] gives the unknown tag it was read with.
    pub fn type_name(&self) -> &str {
        match self {
            Event::Init(_) => "init",
            Event::User(_) => "user",
            Event::Assistant(_) => "assistant",
            Event::PartialAssistant(_) => "partial_assistant",
            Event::Usage(_) => "usage",
            Event::ToolCallStart(_) => "tool_call_start",
            Event::ToolCallEnd(_) => "tool_call_end",
            Event::ToolProgress(_) => "tool_progress",
            Event::PermissionRequest(_) => "permission_request",
            Event::InputRequest(_) => "input_request",
            Event::Suspended(_) => "suspended",
            Event::ContextBuild(_) => "context_build",
            Event::Compaction(_) => "compaction",
            Event::LoopGuard(_) => "loop_guard",
            Event::Budget(_) => "budget",
            Event::PromptCacheAdvisory(_) => "prompt_cache_advisory",
            Event::SkillsLoaded(_) => "skills_loaded",
            Event::SkillInvoked(_) => "skill_invoked",
            Event::SkillCompleted(_) => "skill_completed",
            Event::Subagent(_) => "subagent",
            Event::ChildRunStatus(_) => "child_run_status",
            Event::Workflow(_) => "workflow",
            Event::Verification(_) => "verification",
            Event::Hook(_) => "hook",
            Event::Error(_) => "error",
            Event::Result(_) => "result",
            Event::ModelCallDelivery(_) => "model_call_delivery",
            Event::ModelCallReceipt(_) => "model_call_receipt",
            Event::ModelCallUsageSettlement(_) => "model_call_usage_settlement",
            Event::Ignorable(e) => e.type_name(),
        }
    }
}

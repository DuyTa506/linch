//! Events in their wire form, for streams built by hand with
//! [`super::envelope`]: run `r1` of session `s1` on model `m`.

use serde_json::{Value, json};

pub fn init() -> Value {
    json!({"type": "init", "session_id": "s1", "run_id": "r1", "model": "m", "tools": []})
}

pub fn prompt() -> Value {
    let message = json!({"role": "user", "content": [{"type": "text", "text": "hi"}]});
    json!({"type": "user", "message": message, "subtype": "prompt"})
}

pub fn context_build() -> Value {
    json!({"type": "context_build", "system_blocks": 1, "messages": 1, "budget": {}, "metadata": {}})
}

pub fn assistant(stop_reason: &str) -> Value {
    let message = json!({"role": "assistant", "content": [{"type": "text", "text": "x"}]});
    json!({"type": "assistant", "message": message, "stop_reason": stop_reason})
}

pub fn usage_event(input: u64, output: u64) -> Value {
    let usage = json!({"input_tokens": input, "output_tokens": output});
    json!({"type": "usage", "usage": usage, "cumulative": usage})
}

pub fn tool_start(id: &str) -> Value {
    json!({
        "type": "tool_call_start", "tool_use_id": id, "tool_name": "Echo",
        "input": {}, "summary": "Echo",
    })
}

pub fn tool_end(id: &str, is_error: bool, duration_ms: u64) -> Value {
    let text = json!([{"type": "text", "text": "out"}]);
    json!({
        "type": "tool_call_end", "tool_name": "Echo", "duration_ms": duration_ms,
        "result": {"tool_use_id": id, "content": text, "is_error": is_error},
    })
}

/// The tool results that end a turn.
pub fn tool_results(id: &str) -> Value {
    let result = json!({"type": "tool_result", "tool_use_id": id, "content": []});
    let message = json!({"role": "user", "content": [result]});
    json!({"type": "user", "message": message, "subtype": "tool_result"})
}

pub fn result(subtype: &str) -> Value {
    let zero = json!({"input_tokens": 0, "output_tokens": 0});
    json!({
        "type": "result", "subtype": subtype, "stop_reason": "error",
        "total_usage": zero, "usage_complete": true, "duration_ms": 1,
    })
}

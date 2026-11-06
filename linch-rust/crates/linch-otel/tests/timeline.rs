//! A hand-built stream with a distinct time on every event, so each span's
//! start and end can only come from the event that should set it.

mod common;

use common::*;

#[test]
fn each_span_starts_and_ends_on_its_own_events() {
    let sdk = Sdk::new();
    let mut observer = sdk.observer();
    let (mut paid, mut ended) = (usage_event(10, 5), result("success"));
    paid["cost_usd"] = 0.25.into();
    ended["total_cost_usd"] = 0.25.into();
    let stream = [
        (100, init()),
        (101, prompt()),
        (110, context_build()),
        (150, assistant("tool_use")),
        (151, paid),
        (160, tool_start("t1")),
        (170, tool_end("t1", false, 10)),
        (171, tool_results("t1")),
        (180, context_build()),
        // The provider reported no usage: no usage event follows.
        (200, assistant("end_turn")),
        (205, ended),
    ];
    for (seq, (ts_ms, event)) in stream.into_iter().enumerate() {
        observer.observe(&envelope(seq as u64 + 1, ts_ms, event));
    }
    drop(observer);

    let spans = sdk.spans();
    let window = |name: &str| {
        let found = all(&spans, name);
        let times = found.iter().map(|s| (s.start_time, s.end_time));
        times.collect::<Vec<_>>()
    };
    assert_eq!(window("invoke_agent"), [(at(100), at(205))]);
    assert_eq!(window("step"), [(at(101), at(171)), (at(171), at(205))]);
    assert_eq!(window("chat m"), [(at(110), at(150)), (at(180), at(200))]);
    assert_eq!(window("execute_tool Echo"), [(at(160), at(170))]);

    let chats = all(&spans, "chat m");
    assert_eq!(int(chats[0], "gen_ai.usage.input_tokens"), Some(10));
    assert_eq!(attr(chats[1], "gen_ai.usage.input_tokens"), None);
    let cost = Some(&opentelemetry::Value::F64(0.25));
    assert_eq!(attr(chats[0], "linch.cost_usd"), cost);
    assert_eq!(attr(chats[1], "linch.cost_usd"), None);
    assert_eq!(attr(span(&spans, "invoke_agent"), "linch.cost_usd"), cost);
    let tokens = sdk.points("gen_ai.client.token.usage");
    assert_eq!(tokens.iter().map(|p| p.count).sum::<u64>(), 2);
    let durations = sdk.points("gen_ai.client.operation.duration");
    assert_eq!(durations.len(), 1);
    assert_eq!(durations[0].count, 2);
    assert!((durations[0].sum - 0.060).abs() < 1e-9);
    assert!(spans.iter().all(|s| error_type(s).is_none()));
}

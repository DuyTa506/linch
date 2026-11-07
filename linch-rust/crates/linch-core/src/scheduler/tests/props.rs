//! Property tests over arbitrary batches.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::Duration;

use proptest::prelude::*;
use serde_json::json;

use super::*;

/// One generated call: which tool, and whether it fails.
#[derive(Debug, Clone, Copy)]
enum Kind {
    Read,
    Write,
    Unknown,
}

fn kind() -> impl Strategy<Value = Kind> {
    prop_oneof![Just(Kind::Read), Just(Kind::Write), Just(Kind::Unknown)]
}

/// Tools that count how many calls run at once and flag a write that runs
/// beside anything, or more reads at once than the cap.
struct Gauge {
    running: AtomicUsize,
    broken: AtomicBool,
}

impl Gauge {
    fn tool(gauge: &Arc<Self>, name: &str, scope: ToolScope, cap: usize, log: &Log) -> TestTool {
        let gauge = Arc::clone(gauge);
        TestTool::new(name, scope, log).body(move |input, _| {
            let gauge = Arc::clone(&gauge);
            async move {
                let now = gauge.running.fetch_add(1, Ordering::SeqCst) + 1;
                let limit = if scope == ToolScope::Read { cap } else { 1 };
                if now > limit {
                    gauge.broken.store(true, Ordering::SeqCst);
                }
                let delay = input.get("delay").and_then(JsonValue::as_u64).unwrap_or(0);
                tokio::time::sleep(Duration::from_millis(delay)).await;
                gauge.running.fetch_sub(1, Ordering::SeqCst);
                if input.get("fail").and_then(JsonValue::as_bool) == Some(true) {
                    return Err(ToolOutputError::new("failed"));
                }
                Ok(ToolOutput::new(JsonValue::Object(input)))
            }
            .boxed()
        })
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(64))]

    #[test]
    fn results_map_one_to_one_to_calls_in_order(
        calls in prop::collection::vec((kind(), 0u64..3, any::<bool>()), 0..10),
        cap in 1usize..4,
    ) {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        let log = new_log();
        let fx = Fixture::new(PermissionMode::SkipDangerous);
        let gauge = Arc::new(Gauge {
            running: AtomicUsize::new(0),
            broken: AtomicBool::new(false),
        });
        fx.tool(Gauge::tool(&gauge, "Read", ToolScope::Read, cap, &log));
        fx.tool(Gauge::tool(&gauge, "Write", ToolScope::Write, cap, &log));
        let blocks: Vec<_> = calls
            .iter()
            .enumerate()
            .map(|(i, (kind, delay, fail))| {
                let name = match kind {
                    Kind::Read => "Read",
                    Kind::Write => "Write",
                    Kind::Unknown => "Nope",
                };
                use_block(&format!("c{i}"), name, json!({"delay": delay, "fail": fail}))
            })
            .collect();
        let sink = RecSink::default();

        let results = runtime.block_on(fx.run(&blocks, &sink, cap));

        prop_assert_eq!(results.len(), blocks.len());
        for ((block, result), (kind, _, fail)) in blocks.iter().zip(&results).zip(&calls) {
            prop_assert_eq!(&result.tool_use_id, &block.id);
            let failed = matches!(kind, Kind::Unknown) || *fail;
            prop_assert_eq!(result.is_error, failed);
        }
        let mut finished = sink.finished_ids();
        finished.sort();
        let mut expected: Vec<_> = blocks.iter().map(|block| block.id.clone()).collect();
        expected.sort();
        prop_assert_eq!(finished, expected, "each call finished exactly once");
        prop_assert!(!gauge.broken.load(Ordering::SeqCst), "a write overlapped or reads beat the cap");
    }
}

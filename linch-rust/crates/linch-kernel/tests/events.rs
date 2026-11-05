//! Port of `tests/kernel/test_events.py` and the bus parts of `test_lifecycle.py`.
//!
//! Not ported because the type system makes them unrepresentable:
//! `waterfall next() is single use` (`Next` is consumed by value) and
//! "dispatching a point with the wrong mode" (each mode has its own point type).
//! Python's `CancelledError` propagation tests have no Rust equivalent.

mod common;

use std::sync::{Arc, Mutex};

use common::{Counter, Log, settle};
use linch_kernel::{EmitPoint, EventBus, KernelError, Next, Reply, SerialPoint, WaterfallPoint};

const EVT: EmitPoint<String> = EmitPoint::new("evt");
const ASK: SerialPoint<String, String> = SerialPoint::new("evt");
const WF: WaterfallPoint<Arc<Mutex<Vec<String>>>, String> = WaterfallPoint::new("wf");

fn answer(value: &'static str) -> impl Fn(String) -> Reply<String> + Send + Sync + 'static {
    move |_| Reply::Value(value.to_string())
}

#[tokio::test]
async fn fork_preserves_order_and_owns_independent_registrations() {
    let bus = EventBus::new();
    let first = bus.on_serial(&ASK, answer("first")).unwrap();
    bus.on_serial(&ASK, answer("inherited")).unwrap();
    let fork = bus.fork(&[]);
    first.dispose().await.unwrap();
    let added = fork.on_serial(&ASK.prepend(), answer("local")).unwrap();
    assert_eq!(
        fork.serial(&ASK, String::new()).await.unwrap().as_deref(),
        Some("local")
    );
    assert_eq!(
        bus.serial(&ASK, String::new()).await.unwrap().as_deref(),
        Some("inherited")
    );
    added.dispose().await.unwrap();
    assert_eq!(
        fork.serial(&ASK, String::new()).await.unwrap().as_deref(),
        Some("first")
    );
}

#[tokio::test]
async fn fork_excludes_named_points() {
    let bus = EventBus::new();
    bus.on(&EVT, |_| Reply::Done).unwrap();
    let fork = bus.fork(&["evt"]);
    assert!(bus.has_listeners("evt"));
    assert!(!fork.has_listeners("evt"));
}

#[tokio::test]
async fn emit_isolates_failing_listener() {
    let seen = Log::default();
    let bus = EventBus::new();
    bus.on(&EVT, |_| Reply::Failed(KernelError::msg("listener failed")))
        .unwrap();
    let ok = seen.clone();
    bus.on(&EVT, move |payload: String| {
        ok.push(format!("ok:{payload}"));
        Reply::Done
    })
    .unwrap();
    bus.emit(&EVT, "x".to_string());
    assert_eq!(seen.items(), ["ok:x"]);
}

#[tokio::test]
async fn emit_runs_sync_listeners_inline_and_schedules_async_ones() {
    let seen = Log::default();
    let bus = EventBus::new();
    let late = seen.clone();
    bus.on(&EVT, move |payload: String| {
        let late = late.clone();
        Reply::later(async move {
            late.push(format!("async:{payload}"));
            Ok(())
        })
    })
    .unwrap();
    let inline = seen.clone();
    bus.on(&EVT, move |payload: String| {
        inline.push(format!("sync:{payload}"));
        Reply::Done
    })
    .unwrap();
    bus.emit(&EVT, "x".to_string());
    assert_eq!(seen.items(), ["sync:x"]);
    settle().await;
    assert_eq!(seen.items(), ["sync:x", "async:x"]);
}

#[tokio::test]
async fn serial_bails_on_first_value() {
    let bus = EventBus::new();
    bus.on_serial(&ASK, |_| Reply::Done).unwrap();
    bus.on_serial(&ASK, answer("stop")).unwrap();
    bus.on_serial(&ASK, answer("never")).unwrap();
    let result = bus.serial(&ASK, "x".to_string()).await.unwrap();
    assert_eq!(result.as_deref(), Some("stop"));
}

#[tokio::test]
async fn serial_awaits_async_listeners_and_returns_none_without_a_value() {
    let bus = EventBus::new();
    bus.on_serial(&ASK, |_| Reply::later_value(async { Ok(None) }))
        .unwrap();
    assert_eq!(bus.serial(&ASK, String::new()).await.unwrap(), None);
    bus.on_serial(&ASK, |p: String| {
        Reply::later_value(async move { Ok(Some(format!("got:{p}"))) })
    })
    .unwrap();
    assert_eq!(
        bus.serial(&ASK, "x".into()).await.unwrap().as_deref(),
        Some("got:x")
    );
}

#[tokio::test]
async fn serial_propagates_listener_failure() {
    let bus = EventBus::new();
    bus.on_serial(&ASK, |_| Reply::Failed(KernelError::msg("nope")))
        .unwrap();
    let err = bus.serial(&ASK, String::new()).await.unwrap_err();
    assert_eq!(err.to_string(), "nope");
}

#[tokio::test]
async fn notification_attempts_all_listeners_before_failing() {
    let seen = Log::default();
    let bus = EventBus::new();
    let boom = seen.clone();
    bus.on(&EVT, move |_| {
        let boom = boom.clone();
        Reply::later(async move {
            boom.push("boom");
            Err(KernelError::msg("listener failed"))
        })
    })
    .unwrap();
    let sibling = seen.clone();
    bus.on(&EVT, move |_| {
        let sibling = sibling.clone();
        Reply::later(async move {
            sibling.push("sibling");
            Ok(())
        })
    })
    .unwrap();
    let err = bus
        .notification(&EVT, String::new())
        .dispose()
        .await
        .unwrap_err();
    assert_eq!(err.to_string(), "listener failed");
    assert_eq!(seen.items(), ["boom", "sibling"]);
}

fn wrap(name: &'static str) -> impl Fn(Arc<Mutex<Vec<String>>>, Next<String>) -> Reply<String> {
    move |order, next| {
        Reply::later_value(async move {
            order.lock().unwrap().push(format!("{name}:before"));
            let result = next.run().await;
            order.lock().unwrap().push(format!("{name}:after"));
            result.map(Some)
        })
    }
}

fn terminal(order: Arc<Mutex<Vec<String>>>) -> Next<String> {
    Next::new(move || async move {
        order.lock().unwrap().push("terminal".into());
        Ok("done".to_string())
    })
}

#[tokio::test]
async fn waterfall_composes_outer_to_inner() {
    let bus = EventBus::new();
    bus.on_waterfall(&WF, wrap("outer")).unwrap();
    bus.on_waterfall(&WF, wrap("inner")).unwrap();
    let order = Arc::new(Mutex::new(Vec::new()));
    let result = bus
        .waterfall(&WF, order.clone(), terminal(order.clone()))
        .await;
    assert_eq!(result.unwrap(), "done");
    assert_eq!(
        *order.lock().unwrap(),
        [
            "outer:before",
            "inner:before",
            "terminal",
            "inner:after",
            "outer:after"
        ]
    );
}

#[tokio::test]
async fn waterfall_listener_can_veto_by_not_calling_next() {
    let reached = Counter::default();
    let bus = EventBus::new();
    bus.on_waterfall(&WF, |_, _next| Reply::Value("vetoed".to_string()))
        .unwrap();
    let never = reached.clone();
    bus.on_waterfall(&WF, move |_, next: Next<String>| {
        never.bump();
        Reply::later_value(async move { next.run().await.map(Some) })
    })
    .unwrap();
    let order = Arc::new(Mutex::new(Vec::new()));
    let result = bus
        .waterfall(&WF, order.clone(), terminal(order.clone()))
        .await;
    assert_eq!(result.unwrap(), "vetoed");
    assert_eq!(reached.get(), 0);
    assert!(order.lock().unwrap().is_empty());
}

#[tokio::test]
async fn waterfall_terminal_runs_with_no_listeners() {
    let bus = EventBus::new();
    let order = Arc::new(Mutex::new(Vec::new()));
    let result = bus.waterfall(&WF, order.clone(), terminal(order)).await;
    assert_eq!(result.unwrap(), "done");
}

#[tokio::test]
async fn waterfall_listener_mutates_shared_payload() {
    let bus = EventBus::new();
    bus.on_waterfall(
        &WF,
        |payload: Arc<Mutex<Vec<String>>>, next: Next<String>| {
            payload.lock().unwrap().push("seen".into());
            Reply::later_value(async move { next.run().await.map(Some) })
        },
    )
    .unwrap();
    let payload = Arc::new(Mutex::new(Vec::new()));
    bus.waterfall(&WF, payload.clone(), terminal(payload.clone()))
        .await
        .unwrap();
    assert_eq!(payload.lock().unwrap()[0], "seen");
}

#[tokio::test]
async fn on_returns_disposer_that_unregisters() {
    let seen = Log::default();
    let bus = EventBus::new();
    let a = seen.clone();
    let handle = bus
        .on(&EVT, move |p: String| {
            a.push(format!("a:{p}"));
            Reply::Done
        })
        .unwrap();
    bus.emit(&EVT, "1".into());
    handle.dispose().await.unwrap();
    bus.emit(&EVT, "2".into());
    assert_eq!(seen.items(), ["a:1"]);
    assert!(!bus.has_listeners("evt"));
}

#[tokio::test]
async fn on_prepend_orders_before_existing() {
    let order = Log::default();
    let bus = EventBus::new();
    let second = order.clone();
    bus.on(&EVT, move |_| {
        second.push("second");
        Reply::Done
    })
    .unwrap();
    let first = order.clone();
    bus.on(&EVT.prepend(), move |_| {
        first.push("first");
        Reply::Done
    })
    .unwrap();
    bus.emit(&EVT, String::new());
    assert_eq!(order.items(), ["first", "second"]);
}

#[tokio::test]
async fn disposer_removes_exact_duplicate_registration() {
    let order = Log::default();
    let bus = EventBus::new();
    let tag = |name: &'static str| {
        let order = order.clone();
        move |_: String| {
            order.push(name);
            Reply::Done
        }
    };
    let shared: Arc<dyn Fn(String) -> Reply<()> + Send + Sync> = Arc::new(tag("shared"));
    let call_shared = |shared: &Arc<dyn Fn(String) -> Reply<()> + Send + Sync>| {
        let shared = Arc::clone(shared);
        move |p: String| shared(p)
    };
    bus.on(&EVT, tag("first")).unwrap();
    bus.on(&EVT, call_shared(&shared)).unwrap();
    bus.on(&EVT, tag("middle")).unwrap();
    let later_duplicate = bus.on(&EVT, call_shared(&shared)).unwrap();
    bus.on(&EVT, tag("last")).unwrap();
    later_duplicate.dispose().await.unwrap();
    bus.emit(&EVT, String::new());
    assert_eq!(order.items(), ["first", "shared", "middle", "last"]);
}

#[tokio::test]
async fn one_name_cannot_carry_two_payload_types() {
    let bus = EventBus::new();
    bus.on(&EVT, |_| Reply::Done).unwrap();
    let other: EmitPoint<u32> = EmitPoint::new("evt");
    let err = bus.on(&other, |_| Reply::Done).unwrap_err();
    assert!(matches!(err, KernelError::Config(_)));
}

#[test]
fn points_expose_name_and_mode() {
    use linch_kernel::DispatchMode;
    assert_eq!(EVT.name(), "evt");
    assert_eq!(EVT.mode(), DispatchMode::Emit);
    assert_eq!(ASK.mode(), DispatchMode::Serial);
    assert_eq!(WF.mode(), DispatchMode::Waterfall);
    assert_eq!(linch_kernel::CONTEXT_SERVICE.name(), "context/service");
}

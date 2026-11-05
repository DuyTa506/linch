//! Port of `tests/kernel/test_context.py`.
//!
//! `Context.inbox` is not part of the Rust kernel yet: its delivery target is the
//! agent, so it lands with `Agent` in a later phase.

mod common;

use std::sync::Arc;

use common::{Counter, Gate, Log, settle};
use linch_kernel::{
    CONTEXT_SERVICE, Context, Disposable, EmitPoint, KernelError, Reply, ServiceChange, ServiceKey,
};

#[derive(Debug)]
struct Svc(&'static str);

const EXECUTION: ServiceKey<Svc> = ServiceKey::new("execution");
const EVT: EmitPoint<String> = EmitPoint::new("evt");

fn svc(tag: &'static str) -> Arc<Svc> {
    Arc::new(Svc(tag))
}

#[tokio::test]
async fn registration_and_scopes() {
    let ctx = Context::new();
    let shell = svc("shell");
    EXECUTION.register(&ctx, shell.clone()).unwrap();
    assert!(Arc::ptr_eq(&EXECUTION.require(&ctx).unwrap(), &shell));
    assert!(Arc::ptr_eq(&ctx.get::<Svc>("execution").unwrap(), &shell));
    assert!(ctx.get::<Svc>("nope").is_none());
    assert!(matches!(
        ctx.require::<Svc>("missing"),
        Err(KernelError::MissingService { .. })
    ));
    assert!(matches!(
        ctx.require::<String>("execution"),
        Err(KernelError::ServiceType { .. })
    ));

    let handle = ctx.register("svc", svc("a")).unwrap();
    assert!(ctx.get::<Svc>("svc").is_some());
    handle.dispose().await.unwrap();
    assert!(ctx.get::<Svc>("svc").is_none());
    ctx.register("svc", svc("a")).unwrap();
    assert!(matches!(
        ctx.register("svc", svc("b")),
        Err(KernelError::Config(_))
    ));

    let parent = Context::new();
    EXECUTION.register(&parent, svc("local")).unwrap();
    let child = parent.scope("agent-1", false).unwrap();
    assert_eq!(EXECUTION.require(&child).unwrap().0, "local");
    child.register("agent_only", svc("x")).unwrap();
    assert!(child.get::<Svc>("agent_only").is_some());
    assert!(parent.get::<Svc>("agent_only").is_none());
    EXECUTION.register(&child, svc("remote")).unwrap();
    assert_eq!(EXECUTION.require(&child).unwrap().0, "remote");
    assert_eq!(EXECUTION.require(&parent).unwrap().0, "local");
}

#[tokio::test]
async fn isolated_scope_keeps_local_listeners_and_relays_parent_service_changes() {
    let root = Context::new();
    let child = root.scope("run", true).unwrap();
    let seen = Log::default();
    let sink = seen.clone();
    child
        .on(&EVT, move |value: String| {
            sink.push(value);
            Reply::Done
        })
        .unwrap();
    root.events().emit(&EVT, "other-run".into());
    child.events().emit(&EVT, "this-run".into());
    assert_eq!(seen.items(), ["this-run"]);

    let removed = Gate::new();
    let flag = removed.clone();
    child
        .on(&CONTEXT_SERVICE, move |change: ServiceChange| {
            if change.context.get_any(&change.key).is_none() {
                flag.open();
            }
            Reply::Done
        })
        .unwrap();
    let registration = root.register("dependency", Arc::new(1_u8)).unwrap();
    registration.dispose().await.unwrap();
    assert!(removed.is_open());
    child.dispose().await.unwrap();
    assert!(!root.events().has_listeners(CONTEXT_SERVICE.name()));
}

#[tokio::test]
async fn service_change_event_fires_after_register_and_remove() {
    let ctx = Context::new();
    let seen = Log::default();
    let sink = seen.clone();
    ctx.on(&CONTEXT_SERVICE, move |change: ServiceChange| {
        let present = change.context.get_any(&change.key).is_some();
        sink.push(format!(
            "{}:{}:{present}",
            change.context.label(),
            change.key
        ));
        Reply::Done
    })
    .unwrap();
    let handle = ctx.register("svc", svc("evented")).unwrap();
    assert_eq!(seen.items(), ["root:svc:true"]);
    handle.dispose().await.unwrap();
    assert_eq!(seen.items(), ["root:svc:true", "root:svc:false"]);
}

#[tokio::test]
async fn no_service_event_is_dispatched_without_listeners() {
    let ctx = Context::new();
    let handle = ctx.register("svc", svc("quiet")).unwrap();
    handle.dispose().await.unwrap();
    assert!(!ctx.events().has_listeners(CONTEXT_SERVICE.name()));
}

#[tokio::test]
async fn service_remove_waits_for_async_change_listeners() {
    let ctx = Context::new();
    let started = Gate::new();
    let release = Gate::new();
    let finished = Gate::new();
    {
        let (started, release, finished) = (started.clone(), release.clone(), finished.clone());
        ctx.on(&CONTEXT_SERVICE, move |change: ServiceChange| {
            let (started, release, finished) = (started.clone(), release.clone(), finished.clone());
            Reply::later(async move {
                if change.context.get_any(&change.key).is_none() {
                    started.open();
                    release.wait().await;
                    finished.open();
                }
                Ok(())
            })
        })
        .unwrap();
    }
    let handle = ctx.register("svc", svc("async")).unwrap();
    let disposing = tokio::spawn(async move { handle.dispose().await });
    started.wait().await;
    assert!(!disposing.is_finished());
    assert!(!finished.is_open());
    release.open();
    disposing.await.unwrap().unwrap();
    assert!(finished.is_open());
}

fn failing_once_on_removal(ctx: &Context, attempts: &Counter) {
    let attempts = attempts.clone();
    ctx.on(&CONTEXT_SERVICE, move |change: ServiceChange| {
        let attempts = attempts.clone();
        Reply::later(async move {
            if change.context.get_any(&change.key).is_none() && attempts.bump() == 1 {
                return Err(KernelError::msg("listener failed"));
            }
            Ok(())
        })
    })
    .unwrap();
}

#[tokio::test]
async fn service_remove_retries_failed_change_notification() {
    let ctx = Context::new();
    let attempts = Counter::default();
    failing_once_on_removal(&ctx, &attempts);
    let handle = ctx.register("svc", svc("retry")).unwrap();
    let err = handle.dispose().await.unwrap_err();
    assert_eq!(err.to_string(), "listener failed");
    assert!(ctx.get_any("svc").is_none());
    handle.dispose().await.unwrap();
    assert_eq!(attempts.get(), 2);
}

#[tokio::test]
async fn context_dispose_retries_failed_notification_after_listener_teardown() {
    let ctx = Context::new();
    let failed = Counter::default();
    let ok = Counter::default();
    failing_once_on_removal(&ctx, &failed);
    {
        let ok = ok.clone();
        ctx.on(&CONTEXT_SERVICE, move |change: ServiceChange| {
            if change.context.get_any(&change.key).is_none() {
                ok.bump();
            }
            Reply::Done
        })
        .unwrap();
    }
    ctx.register("svc", svc("context-retry")).unwrap();
    settle().await;
    let err = ctx.dispose().await.unwrap_err();
    assert_eq!(err.to_string(), "listener failed");
    assert_eq!((failed.get(), ok.get()), (1, 1));
    ctx.dispose().await.unwrap();
    assert_eq!((failed.get(), ok.get()), (2, 1));
}

fn blocking_effect(ctx: &Context, started: &Arc<Gate>, release: &Arc<Gate>) {
    let (started, release) = (started.clone(), release.clone());
    ctx.effect(move || {
        Disposable::new(move || {
            let (started, release) = (started.clone(), release.clone());
            async move {
                started.open();
                release.wait().await;
                Ok(())
            }
        })
    })
    .unwrap();
}

#[tokio::test]
async fn context_hides_services_while_disposal_is_running() {
    let ctx = Context::new();
    ctx.register("svc", svc("hidden")).unwrap();
    let (started, release) = (Gate::new(), Gate::new());
    blocking_effect(&ctx, &started, &release);
    let disposing = tokio::spawn({
        let ctx = ctx.clone();
        async move { ctx.dispose().await }
    });
    started.wait().await;
    assert!(ctx.get_any("svc").is_none());
    release.open();
    disposing.await.unwrap().unwrap();
}

#[tokio::test]
async fn disposed_child_context_is_released_before_parent_disposal() {
    let parent = Context::new();
    let child = parent.scope("temporary", false).unwrap();
    let weak = child.downgrade();
    child.dispose().await.unwrap();
    drop(child);
    assert!(weak.upgrade().is_none());
    parent.dispose().await.unwrap();
}

#[tokio::test]
async fn disposing_parent_disposes_child_scope() {
    let parent = Context::new();
    let child = parent.scope("agent-1", false).unwrap();
    let removed = Log::default();
    child.on(&EVT, |_| Reply::Done).unwrap();
    let sink = removed.clone();
    child
        .effect(move || {
            Disposable::sync(move || {
                sink.push("child-effect");
                Ok(())
            })
        })
        .unwrap();
    parent.dispose().await.unwrap();
    assert_eq!(removed.items(), ["child-effect"]);
    assert!(!parent.events().has_listeners("evt"));
}

#[tokio::test]
async fn child_listeners_share_parent_bus_and_teardown_with_child() {
    let parent = Context::new();
    let child = parent.scope("agent-1", false).unwrap();
    let seen = Log::default();
    let sink = seen.clone();
    child
        .on(&EVT, move |p: String| {
            sink.push(format!("child:{p}"));
            Reply::Done
        })
        .unwrap();
    parent.events().emit(&EVT, "1".into());
    assert_eq!(seen.items(), ["child:1"]);
    child.dispose().await.unwrap();
    parent.events().emit(&EVT, "2".into());
    assert_eq!(seen.items(), ["child:1"]);
}

#[tokio::test]
async fn two_contexts_are_isolated() {
    let (a, b) = (Context::new(), Context::new());
    a.register("svc", svc("a")).unwrap();
    b.register("svc", svc("b")).unwrap();
    assert_eq!(a.get::<Svc>("svc").unwrap().0, "a");
    assert_eq!(b.get::<Svc>("svc").unwrap().0, "b");
}

fn assert_closed<T: std::fmt::Debug>(result: Result<T, KernelError>) {
    assert!(matches!(result, Err(KernelError::Closed)), "{result:?}");
}

#[tokio::test]
async fn context_rejects_mutation_after_dispose() {
    let ctx = Context::new();
    ctx.dispose().await.unwrap();
    assert_closed(ctx.register("svc", svc("late")));
    assert_closed(ctx.on(&EVT, |_| Reply::Done));
    assert_closed(ctx.effect(|| ()));
    assert_closed(ctx.scope("late", false));
    assert!(!ctx.events().has_listeners("evt"));
}

#[tokio::test]
async fn context_rejects_mutation_while_disposing() {
    let ctx = Context::new();
    ctx.register("svc", svc("existing")).unwrap();
    let (started, release) = (Gate::new(), Gate::new());
    blocking_effect(&ctx, &started, &release);
    let disposing = tokio::spawn({
        let ctx = ctx.clone();
        async move { ctx.dispose().await }
    });
    started.wait().await;
    let late: EmitPoint<String> = EmitPoint::new("late");
    assert_closed(ctx.on(&late, |_| Reply::Done));
    assert_closed(ctx.register("svc", svc("replacement")));
    assert!(!ctx.events().has_listeners("late"));
    release.open();
    disposing.await.unwrap().unwrap();
}

#[tokio::test]
async fn context_dispose_continues_after_cleanup_failure() {
    let ctx = Context::new();
    let seen = Log::default();
    ctx.register("svc", svc("owned")).unwrap();
    let sink = seen.clone();
    ctx.on(&EVT, move |_| {
        sink.push("listener");
        Reply::Done
    })
    .unwrap();
    ctx.effect(|| Disposable::sync(|| Err(KernelError::msg("cleanup failed"))))
        .unwrap();
    let err = ctx.dispose().await.unwrap_err();
    assert_eq!(err.to_string(), "cleanup failed");
    assert!(ctx.get_any("svc").is_none());
    ctx.events().emit(&EVT, String::new());
    assert!(seen.items().is_empty());
}

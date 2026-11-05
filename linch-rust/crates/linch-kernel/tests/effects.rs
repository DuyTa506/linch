//! Port of `tests/kernel/test_effects.py`.

mod common;

use common::{Counter, Gate, Log};
use linch_kernel::{Disposable, EffectScope, KernelError};

fn push(log: &Log, item: &'static str) -> Disposable {
    let log = log.clone();
    Disposable::sync(move || {
        log.push(item);
        Ok(())
    })
}

#[tokio::test]
async fn disposable_runs_once() {
    let log = Log::default();
    let d = push(&log, "1");
    d.dispose().await.unwrap();
    d.dispose().await.unwrap();
    d.dispose().await.unwrap();
    assert_eq!(log.items(), ["1"]);
    assert!(d.is_disposed());
}

#[tokio::test]
async fn effect_disposes_in_reverse_order() {
    let log = Log::default();
    let scope = EffectScope::new();
    for item in ["0", "1", "2"] {
        let log = log.clone();
        scope.effect(move || push(&log, item)).unwrap();
    }
    scope.dispose().await.unwrap();
    assert_eq!(log.items(), ["2", "1", "0"]);
}

#[tokio::test]
async fn effect_group_disposer_is_scoped() {
    let log = Log::default();
    let scope = EffectScope::new();
    let outer = log.clone();
    scope.effect(move || push(&outer, "outer")).unwrap();
    let inner = log.clone();
    let handle = scope.effect(move || push(&inner, "inner")).unwrap();
    handle.dispose().await.unwrap();
    assert_eq!(log.items(), ["inner"]);
    scope.dispose().await.unwrap();
    assert_eq!(log.items(), ["inner", "outer"]);
}

#[tokio::test]
async fn effect_accepts_a_group_of_disposers() {
    let log = Log::default();
    let scope = EffectScope::new();
    let group = log.clone();
    scope
        .effect(move || vec![push(&group, "1"), push(&group, "2")])
        .unwrap();
    scope.dispose().await.unwrap();
    assert_eq!(log.items(), ["2", "1"]);
}

#[tokio::test]
async fn async_teardown_and_scope_idempotence() {
    let calls = Counter::default();
    let make = |calls: &Counter| {
        let calls = calls.clone();
        Disposable::new(move || {
            let calls = calls.clone();
            async move {
                tokio::task::yield_now().await;
                calls.bump();
                Ok(())
            }
        })
    };
    make(&calls).dispose().await.unwrap();
    let scope = EffectScope::new();
    let inner = calls.clone();
    scope.effect(move || make(&inner)).unwrap();
    scope.dispose().await.unwrap();
    scope.dispose().await.unwrap();
    assert_eq!(calls.get(), 2);
}

#[tokio::test]
async fn concurrent_dispose_coalesces_and_shields_teardown() {
    let started = Gate::new();
    let release = Gate::new();
    let calls = Counter::default();
    let d = {
        let (started, release, calls) = (started.clone(), release.clone(), calls.clone());
        Disposable::new(move || {
            let (started, release, calls) = (started.clone(), release.clone(), calls.clone());
            async move {
                calls.bump();
                started.open();
                release.wait().await;
                Ok(())
            }
        })
    };
    let cancelled = tokio::spawn({
        let d = d.clone();
        async move { d.dispose().await }
    });
    started.wait().await;
    let other = tokio::spawn({
        let d = d.clone();
        async move { d.dispose().await }
    });
    cancelled.abort();
    assert!(cancelled.await.unwrap_err().is_cancelled());
    release.open();
    other.await.unwrap().unwrap();
    assert_eq!(calls.get(), 1);
    assert!(d.is_disposed());
}

#[tokio::test]
async fn cleanup_failure_is_shared_then_retryable() {
    let started = Gate::new();
    let release = Gate::new();
    let attempts = Counter::default();
    let d = {
        let (started, release, attempts) = (started.clone(), release.clone(), attempts.clone());
        Disposable::new(move || {
            let (started, release, attempts) = (started.clone(), release.clone(), attempts.clone());
            async move {
                if attempts.bump() == 1 {
                    started.open();
                    release.wait().await;
                    return Err(KernelError::msg("cleanup failed"));
                }
                Ok(())
            }
        })
    };
    let first = tokio::spawn({
        let d = d.clone();
        async move { d.dispose().await }
    });
    started.wait().await;
    let second = tokio::spawn({
        let d = d.clone();
        async move { d.dispose().await }
    });
    release.open();
    for result in [first.await.unwrap(), second.await.unwrap()] {
        assert_eq!(result.unwrap_err().to_string(), "cleanup failed");
    }
    assert_eq!(attempts.get(), 1);
    assert!(!d.is_disposed());
    d.dispose().await.unwrap();
    assert_eq!(attempts.get(), 2);
    assert!(d.is_disposed());
}

#[tokio::test]
async fn scope_attempts_all_cleanup_and_retries_only_failures() {
    let log = Log::default();
    let failed_once = Counter::default();
    let scope = EffectScope::new();
    scope.add(push(&log, "first")).unwrap();
    let flaky = {
        let (log, failed_once) = (log.clone(), failed_once.clone());
        Disposable::sync(move || {
            log.push("flaky");
            if failed_once.bump() == 1 {
                return Err(KernelError::msg("try again"));
            }
            Ok(())
        })
    };
    scope.add(flaky).unwrap();
    scope.add(push(&log, "last")).unwrap();
    let err = scope.dispose().await.unwrap_err();
    assert_eq!(err.to_string(), "try again");
    assert_eq!(log.items(), ["last", "flaky", "first"]);
    assert!(scope.is_closed());
    assert!(!scope.is_disposed());
    scope.dispose().await.unwrap();
    assert_eq!(log.items(), ["last", "flaky", "first", "flaky"]);
    assert!(scope.is_disposed());
}

#[tokio::test]
async fn multiple_failures_are_collected() {
    let scope = EffectScope::new();
    for _ in 0..2 {
        scope
            .add(Disposable::sync(|| Err(KernelError::msg("boom"))))
            .unwrap();
    }
    let err = scope.dispose().await.unwrap_err();
    assert!(matches!(&err, KernelError::Cleanup(errors) if errors.len() == 2));
    assert_eq!(err.to_string(), "2 cleanup operations failed");
}

#[tokio::test]
async fn scope_rejects_add_and_effect_once_teardown_starts() {
    let started = Gate::new();
    let release = Gate::new();
    let scope = EffectScope::new();
    {
        let (started, release) = (started.clone(), release.clone());
        scope
            .add(Disposable::new(move || {
                let (started, release) = (started.clone(), release.clone());
                async move {
                    started.open();
                    release.wait().await;
                    Ok(())
                }
            }))
            .unwrap();
    }
    let disposing = tokio::spawn({
        let scope = scope.clone();
        async move { scope.dispose().await }
    });
    started.wait().await;
    let err = scope.add(Disposable::noop()).unwrap_err();
    assert!(err.to_string().contains("disposing or disposed"));
    let setup_called = Counter::default();
    let called = setup_called.clone();
    let err = scope
        .effect(move || {
            called.bump();
        })
        .unwrap_err();
    assert!(matches!(err, KernelError::Closed));
    assert_eq!(setup_called.get(), 0);
    release.open();
    disposing.await.unwrap().unwrap();
}

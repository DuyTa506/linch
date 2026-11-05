//! Port of `tests/kernel/test_plugins.py`.
//!
//! The Python tests mount on `agent.context`; here a plain child context stands
//! in for it (`Agent` arrives in a later phase). `inbox` escape checks wait for
//! `Context::inbox`, which lands with `Agent`.

mod common;

use std::any::Any;
use std::sync::{Arc, Mutex};

use common::{Counter, Gate, Log, settle};
use futures::future::BoxFuture;
use linch_kernel::{Context, Disposable, KernelError, Plugin, PluginMount, mount_plugin};

type Service = Arc<dyn Any + Send + Sync>;
type Factory = Arc<dyn Fn(&Context) -> Service + Send + Sync>;
type Cleanup = Arc<dyn Fn() -> BoxFuture<'static, Result<(), KernelError>> + Send + Sync>;

fn object() -> Service {
    Arc::new(())
}

fn agent(parent: Option<&Context>) -> Context {
    match parent {
        Some(parent) => parent.scope("agent", false).unwrap(),
        None => Context::new().scope("agent", false).unwrap(),
    }
}

/// Mirrors the Python `_Plugin` test double.
struct TestPlugin {
    name: &'static str,
    provides: Vec<String>,
    requires: Vec<String>,
    calls: Counter,
    cleanups: Counter,
    factory: Factory,
    cleanup: Option<Cleanup>,
}

impl TestPlugin {
    fn new(name: &'static str, provides: &[&str], requires: &[&str]) -> Self {
        Self {
            name,
            provides: provides.iter().map(|s| s.to_string()).collect(),
            requires: requires.iter().map(|s| s.to_string()).collect(),
            calls: Counter::default(),
            cleanups: Counter::default(),
            factory: Arc::new(|_| object()),
            cleanup: None,
        }
    }

    fn factory(mut self, factory: impl Fn(&Context) -> Service + Send + Sync + 'static) -> Self {
        self.factory = Arc::new(factory);
        self
    }

    fn cleanup<F, Fut>(mut self, cleanup: F) -> Self
    where
        F: Fn() -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<(), KernelError>> + Send + 'static,
    {
        self.cleanup = Some(Arc::new(move || Box::pin(cleanup())));
        self
    }
}

impl Plugin for TestPlugin {
    type Config = ();

    fn name(&self) -> &str {
        self.name
    }

    fn version(&self) -> &str {
        "1"
    }

    fn provides(&self) -> Vec<String> {
        self.provides.clone()
    }

    fn requires(&self) -> Vec<String> {
        self.requires.clone()
    }

    fn setup(&self, ctx: &Context, _config: &()) -> Result<Disposable, KernelError> {
        self.calls.bump();
        let services = self
            .provides
            .iter()
            .map(|key| ctx.register_any(key, (self.factory)(ctx)))
            .collect::<Result<Vec<_>, _>>()?;
        let (cleanups, cleanup) = (self.cleanups.clone(), self.cleanup.clone());
        Ok(Disposable::new(move || {
            let (cleanups, cleanup, services) =
                (cleanups.clone(), cleanup.clone(), services.clone());
            async move {
                cleanups.bump();
                if let Some(cleanup) = cleanup {
                    cleanup().await?;
                }
                for service in services.iter().rev() {
                    service.dispose().await?;
                }
                Ok(())
            }
        }))
    }
}

/// Mirrors `_RegistrationPlugin`: registers one key and exposes the handle.
struct RegistrationPlugin {
    name: &'static str,
    key: &'static str,
    requires: Vec<String>,
    return_registration: bool,
    registration: Mutex<Option<Disposable>>,
    source: Mutex<Option<Context>>,
}

impl RegistrationPlugin {
    fn new(name: &'static str, key: &'static str, return_registration: bool) -> Self {
        Self {
            name,
            key,
            requires: Vec::new(),
            return_registration,
            registration: Mutex::new(None),
            source: Mutex::new(None),
        }
    }

    fn registration(&self) -> Disposable {
        self.registration.lock().unwrap().clone().unwrap()
    }
}

impl Plugin for RegistrationPlugin {
    type Config = ();

    fn name(&self) -> &str {
        self.name
    }

    fn version(&self) -> &str {
        "1"
    }

    fn provides(&self) -> Vec<String> {
        vec![self.key.to_string()]
    }

    fn requires(&self) -> Vec<String> {
        self.requires.clone()
    }

    fn setup(&self, ctx: &Context, _config: &()) -> Result<Disposable, KernelError> {
        *self.source.lock().unwrap() = Some(ctx.clone());
        let registration = ctx.register_any(self.key, object())?;
        *self.registration.lock().unwrap() = Some(registration.clone());
        Ok(if self.return_registration {
            registration
        } else {
            Disposable::noop()
        })
    }
}

/// A plugin whose setup runs an arbitrary action, like `_ActionPlugin`.
struct ActionPlugin<F>(&'static str, F);

impl<F> Plugin for ActionPlugin<F>
where
    F: Fn(&Context) -> Result<Disposable, KernelError> + Send + Sync + 'static,
{
    type Config = ();

    fn name(&self) -> &str {
        self.0
    }

    fn version(&self) -> &str {
        "1"
    }

    fn provides(&self) -> Vec<String> {
        Vec::new()
    }

    fn requires(&self) -> Vec<String> {
        Vec::new()
    }

    fn setup(&self, ctx: &Context, _config: &()) -> Result<Disposable, KernelError> {
        (self.1)(ctx)
    }
}

fn blocked(
    started: &Arc<Gate>,
    release: &Arc<Gate>,
) -> impl Fn() -> BoxFuture<'static, Result<(), KernelError>> + Send + Sync + 'static {
    let (started, release) = (started.clone(), release.clone());
    move || {
        let (started, release) = (started.clone(), release.clone());
        Box::pin(async move {
            started.open();
            release.wait().await;
            Ok(())
        })
    }
}

fn is_config(err: &KernelError, needle: &str) -> bool {
    matches!(err, KernelError::Config(message) if message.contains(needle))
}

#[tokio::test]
async fn dormant_activation_removal_and_reactivation() {
    let ctx = agent(None);
    let plugin = Arc::new(TestPlugin::new("plugin", &["out"], &["dep"]));
    let mounted = mount_plugin(&ctx, plugin.clone(), ()).await.unwrap();
    assert_eq!(plugin.calls.get(), 0);

    let dependency = ctx.register("dep", Arc::new(0_u8)).unwrap();
    settle().await;
    assert_eq!(plugin.calls.get(), 1);
    assert!(ctx.get_any("out").is_some());

    dependency.dispose().await.unwrap();
    assert!(ctx.get_any("out").is_none());
    assert_eq!(plugin.cleanups.get(), 1);

    let _dependency = ctx.register("dep", Arc::new(false)).unwrap();
    settle().await;
    assert_eq!(plugin.calls.get(), 2);
    assert!(ctx.get_any("out").is_some());

    mounted.dispose().await.unwrap();
    assert!(ctx.get_any("out").is_none());
}

#[tokio::test]
async fn dependency_removal_rebinds_to_inherited_fallback() {
    let parent = Context::new();
    let inherited = object();
    parent.register_any("dep", inherited.clone()).unwrap();
    let ctx = agent(Some(&parent));
    let shadow = ctx.register_any("dep", object()).unwrap();
    let seen: Arc<Mutex<Vec<Service>>> = Arc::default();
    let sink = seen.clone();
    let plugin = TestPlugin::new("binding", &["out"], &["dep"]).factory(move |ctx| {
        sink.lock().unwrap().push(ctx.get_any("dep").unwrap());
        object()
    });
    let mounted = mount_plugin(&ctx, Arc::new(plugin), ()).await.unwrap();
    shadow.dispose().await.unwrap();
    settle().await;
    let seen = seen.lock().unwrap().clone();
    assert_eq!(seen.len(), 2);
    assert!(!Arc::ptr_eq(&seen[0], &inherited));
    assert!(Arc::ptr_eq(&seen[1], &inherited));
    mounted.dispose().await.unwrap();
}

#[tokio::test]
async fn dependencies_chain_and_unmount_cascade() {
    let ctx = agent(None);
    let first = Arc::new(TestPlugin::new("first", &["first"], &[]));
    let second = Arc::new(TestPlugin::new("second", &["second"], &["first"]));
    let first_mount = mount_plugin(&ctx, first.clone(), ()).await.unwrap();
    let second_mount = mount_plugin(&ctx, second.clone(), ()).await.unwrap();
    assert_eq!((first.calls.get(), second.calls.get()), (1, 1));
    assert!(ctx.get_any("second").is_some());

    first_mount.dispose().await.unwrap();
    assert!(ctx.get_any("first").is_none());
    assert!(ctx.get_any("second").is_none());
    assert_eq!(second.cleanups.get(), 1);

    mount_plugin(&ctx, first.clone(), ()).await.unwrap();
    settle().await;
    assert_eq!((first.calls.get(), second.calls.get()), (2, 2));
    assert!(ctx.get_any("second").is_some());
    second_mount.dispose().await.unwrap();
}

#[tokio::test]
async fn disposing_plugin_service_registration_withdraws_export_and_dependents() {
    let ctx = agent(None);
    let manual = Arc::new(RegistrationPlugin::new("manual", "manual", false));
    let dependent = Arc::new(TestPlugin::new("dependent", &["dependent"], &["manual"]));
    mount_plugin(&ctx, manual.clone(), ()).await.unwrap();
    mount_plugin(&ctx, dependent.clone(), ()).await.unwrap();
    assert!(ctx.get_any("dependent").is_some());

    manual.registration().dispose().await.unwrap();
    settle().await;
    assert!(ctx.get_any("manual").is_none());
    assert!(ctx.get_any("dependent").is_none());
    assert_eq!(dependent.cleanups.get(), 1);
}

#[tokio::test]
async fn stale_plugin_export_does_not_hide_inherited_dependency_change() {
    let parent = Context::new();
    let inherited = object();
    let parent_registration = parent.register_any("dep", inherited.clone()).unwrap();
    let ctx = agent(Some(&parent));
    let seen: Arc<Mutex<Vec<Service>>> = Arc::default();
    let manual = Arc::new(RegistrationPlugin::new("manual-dep", "dep", false));
    let sink = seen.clone();
    let dependent =
        TestPlugin::new("fallback-dependent", &["dependent"], &["dep"]).factory(move |ctx| {
            sink.lock().unwrap().push(ctx.get_any("dep").unwrap());
            object()
        });
    let manual_mount = mount_plugin(&ctx, manual.clone(), ()).await.unwrap();
    let dependent_mount = mount_plugin(&ctx, Arc::new(dependent), ()).await.unwrap();
    manual.registration().dispose().await.unwrap();
    settle().await;
    assert!(Arc::ptr_eq(
        seen.lock().unwrap().last().unwrap(),
        &inherited
    ));

    parent_registration.dispose().await.unwrap();
    settle().await;
    assert!(ctx.get_any("dependent").is_none());

    dependent_mount.dispose().await.unwrap();
    manual_mount.dispose().await.unwrap();
    ctx.dispose().await.unwrap();
}

#[tokio::test]
async fn audit_rejects_undeclared_service_and_rolls_back() {
    let ctx = agent(None);
    let cleaned = Gate::new();
    let flag = cleaned.clone();
    let bad = ActionPlugin("bad", move |ctx: &Context| {
        ctx.register_any("leak", object())?;
        let flag = flag.clone();
        Ok(Disposable::sync(move || {
            flag.open();
            Ok(())
        }))
    });
    let err = mount_plugin(&ctx, Arc::new(bad), ()).await.unwrap_err();
    assert!(is_config(&err, "provides"), "{err}");
    assert!(ctx.get_any("leak").is_none());
    assert!(cleaned.is_open());
}

#[tokio::test]
async fn setup_failure_rolls_back_owned_async_cleanup_before_mount_fails() {
    let ctx = agent(None);
    let cleaned = Gate::new();
    let scope: Arc<Mutex<Option<Context>>> = Arc::default();
    let (flag, slot) = (cleaned.clone(), scope.clone());
    let failing = ActionPlugin("failing", move |ctx: &Context| {
        *slot.lock().unwrap() = Some(ctx.clone());
        let flag = flag.clone();
        ctx.effect(move || {
            Disposable::new(move || {
                let flag = flag.clone();
                async move {
                    tokio::task::yield_now().await;
                    flag.open();
                    Ok(())
                }
            })
        })?;
        Err(KernelError::msg("setup failed"))
    });
    let err = mount_plugin(&ctx, Arc::new(failing), ()).await.unwrap_err();
    assert_eq!(err.to_string(), "setup failed");
    assert!(cleaned.is_open());
    assert!(ctx.get_any("leak").is_none());
    assert!(scope.lock().unwrap().as_ref().unwrap().is_disposed());
}

#[tokio::test]
async fn closed_plugin_source_is_not_returned_from_stale_export() {
    let ctx = agent(None);
    let source = Arc::new(RegistrationPlugin::new("source", "out", true));
    mount_plugin(&ctx, source.clone(), ()).await.unwrap();
    let source_ctx = source.source.lock().unwrap().clone().unwrap();
    source_ctx.dispose().await.unwrap();
    assert!(ctx.get_any("out").is_none());
}

#[tokio::test]
async fn setup_cannot_mutate_captured_parent_through_any_context_surface() {
    use linch_kernel::{EmitPoint, Reply};
    const OUTSIDE: EmitPoint<()> = EmitPoint::new("outside");
    let parent = Context::new();
    let ctx = agent(Some(&parent));
    type Op = Box<dyn Fn(&Context) -> Result<(), KernelError> + Send + Sync>;
    let operations: Vec<(&str, Op)> = vec![
        (
            "register",
            Box::new(|p| p.register_any("outside", object()).map(drop)),
        ),
        (
            "on",
            Box::new(|p| p.on(&OUTSIDE, |_| Reply::Done).map(drop)),
        ),
        ("effect", Box::new(|p| p.effect(|| ()).map(drop))),
        ("scope", Box::new(|p| p.scope("outside", false).map(drop))),
    ];
    for (name, op) in operations {
        let target = parent.clone();
        let plugin = ActionPlugin("escape", move |_ctx: &Context| {
            op(&target)?;
            Ok(Disposable::noop())
        });
        let err = mount_plugin(&ctx, Arc::new(plugin), ()).await.unwrap_err();
        assert!(is_config(&err, "activation scope"), "{name}: {err}");
    }
    assert!(parent.get_any("outside").is_none());
    assert!(!parent.events().has_listeners("outside"));
}

#[tokio::test]
async fn rejects_a_key_declared_as_both_requirement_and_provision() {
    let ctx = agent(None);
    let dependency = ctx.register_any("loop", object()).unwrap();
    let plugin = Arc::new(TestPlugin::new("circular", &["loop"], &["loop"]));
    let err = mount_plugin(&ctx, plugin.clone(), ()).await.unwrap_err();
    assert!(is_config(&err, "cycle"), "{err}");
    assert_eq!(plugin.calls.get(), 0);
    dependency.dispose().await.unwrap();
    assert!(ctx.get_any("loop").is_none());
}

#[tokio::test]
async fn unmount_withdraws_exports_before_async_cleanup() {
    let ctx = agent(None);
    let (started, release) = (Gate::new(), Gate::new());
    let observed_absent = Gate::new();
    let host = ctx.clone();
    let (s, r, absent) = (started.clone(), release.clone(), observed_absent.clone());
    let plugin = ActionPlugin("slow", move |scope: &Context| {
        let registration = scope.register_any("slow", object())?;
        let (host, s, r, absent) = (host.clone(), s.clone(), r.clone(), absent.clone());
        scope.effect(move || {
            Disposable::new(move || {
                let (host, s, r, absent) = (host.clone(), s.clone(), r.clone(), absent.clone());
                async move {
                    if host.get_any("slow").is_none() {
                        absent.open();
                    }
                    s.open();
                    r.wait().await;
                    Ok(())
                }
            })
        })?;
        Ok(registration)
    });
    struct Provides<P>(P);
    impl<P: Plugin<Config = ()>> Plugin for Provides<P> {
        type Config = ();
        fn name(&self) -> &str {
            self.0.name()
        }
        fn version(&self) -> &str {
            "1"
        }
        fn provides(&self) -> Vec<String> {
            vec!["slow".into()]
        }
        fn requires(&self) -> Vec<String> {
            Vec::new()
        }
        fn setup(&self, ctx: &Context, config: &()) -> Result<Disposable, KernelError> {
            self.0.setup(ctx, config)
        }
    }
    let mounted = mount_plugin(&ctx, Arc::new(Provides(plugin)), ())
        .await
        .unwrap();
    assert!(ctx.get_any("slow").is_some());
    let disposing = tokio::spawn(async move { mounted.dispose().await });
    started.wait().await;
    assert!(ctx.get_any("slow").is_none());
    assert!(observed_absent.is_open());
    release.open();
    disposing.await.unwrap().unwrap();
}

#[tokio::test]
async fn rejects_setup_escape_to_unrelated_agent_context() {
    let parent = Context::new();
    let ctx = agent(Some(&parent));
    let unrelated = agent(Some(&parent));
    let target = unrelated.clone();
    let plugin = ActionPlugin("unrelated-escape", move |_ctx: &Context| {
        target.register_any("outside", object())?;
        Ok(Disposable::noop())
    });
    let err = mount_plugin(&ctx, Arc::new(plugin), ()).await.unwrap_err();
    assert!(is_config(&err, "activation scope"), "{err}");
    assert!(unrelated.get_any("outside").is_none());
}

#[tokio::test]
async fn cleanup_retry_withdraws_services_before_cleanup() {
    let ctx = agent(None);
    let attempts = Counter::default();
    let counter = attempts.clone();
    let plugin = TestPlugin::new("plugin", &["out"], &[]).cleanup(move || {
        let counter = counter.clone();
        async move {
            if counter.bump() == 1 {
                return Err(KernelError::msg("cleanup failed"));
            }
            Ok(())
        }
    });
    let mounted = mount_plugin(&ctx, Arc::new(plugin), ()).await.unwrap();
    assert!(ctx.get_any("out").is_some());
    let err = mounted.dispose().await.unwrap_err();
    assert_eq!(err.to_string(), "cleanup failed");
    assert!(ctx.get_any("out").is_none());
    mounted.dispose().await.unwrap();
    assert_eq!(attempts.get(), 2);
}

#[tokio::test]
async fn closed_mount_never_reactivates_after_blocked_cleanup() {
    let ctx = agent(None);
    let (started, release) = (Gate::new(), Gate::new());
    let block = blocked(&started, &release);
    let plugin = Arc::new(TestPlugin::new("plugin", &["out"], &["dep"]).cleanup(block));
    let mounted = mount_plugin(&ctx, plugin.clone(), ()).await.unwrap();
    let dependency = ctx.register_any("dep", object()).unwrap();
    settle().await;
    let removal = tokio::spawn(async move { dependency.dispose().await });
    started.wait().await;
    let unmount = tokio::spawn(async move { mounted.dispose().await });
    let replacement = ctx.register_any("dep", object()).unwrap();
    settle().await;
    assert_eq!(plugin.calls.get(), 1);

    release.open();
    removal.await.unwrap().unwrap();
    unmount.await.unwrap().unwrap();
    assert!(ctx.get_any("out").is_none());
    replacement.dispose().await.unwrap();
}

#[tokio::test]
async fn plugins_are_isolated_between_agents_sharing_parent() {
    let parent = Context::new();
    let (first, second) = (agent(Some(&parent)), agent(Some(&parent)));
    let plugin = Arc::new(TestPlugin::new("plugin", &["out"], &[]));
    let first_mount = mount_plugin(&first, plugin.clone(), ()).await.unwrap();
    let second_mount = mount_plugin(&second, plugin.clone(), ()).await.unwrap();
    assert!(first.get_any("out").is_some());
    assert!(second.get_any("out").is_some());
    assert!(parent.get_any("out").is_none());

    first_mount.dispose().await.unwrap();
    assert!(first.get_any("out").is_none());
    assert!(second.get_any("out").is_some());
    second_mount.dispose().await.unwrap();
}

#[tokio::test]
async fn duplicate_provision_rolls_back_second_mount() {
    let ctx = agent(None);
    let first = Arc::new(TestPlugin::new("first", &["same"], &[]));
    let second = Arc::new(TestPlugin::new("second", &["same"], &[]));
    let first_mount = mount_plugin(&ctx, first, ()).await.unwrap();
    let err = mount_plugin(&ctx, second.clone(), ()).await.unwrap_err();
    assert!(is_config(&err, "already provided"), "{err}");
    assert!(ctx.get_any("same").is_some());
    assert_eq!(second.cleanups.get(), 1);
    first_mount.dispose().await.unwrap();
}

#[tokio::test]
async fn indirect_dependency_cycle_is_rejected_before_setup_repeats() {
    let parent = Context::new();
    parent.register_any("a", object()).unwrap();
    parent.register_any("b", object()).unwrap();
    let ctx = agent(Some(&parent));
    let first = Arc::new(TestPlugin::new("first", &["a"], &["b"]));
    let second = Arc::new(TestPlugin::new("second", &["b"], &["a"]));
    mount_plugin(&ctx, first.clone(), ()).await.unwrap();
    let err = mount_plugin(&ctx, second.clone(), ()).await.unwrap_err();
    assert!(is_config(&err, "cycle"), "{err}");
    settle().await;
    assert_eq!((first.calls.get(), second.calls.get()), (1, 0));
    assert!(ctx.get_any("a").is_some());
    assert!(ctx.get_any("b").is_some());
}

#[tokio::test]
async fn closed_dependent_retries_before_provider_cleanup_finishes() {
    let ctx = agent(None);
    let attempts = Counter::default();
    let order = Log::default();
    let (counter, dep_order) = (attempts.clone(), order.clone());
    let dependent = TestPlugin::new("dependent-retry", &[], &["a"]).cleanup(move || {
        let (counter, dep_order) = (counter.clone(), dep_order.clone());
        async move {
            if counter.bump() < 3 {
                return Err(KernelError::msg("dependent cleanup failed"));
            }
            dep_order.push("dependent");
            Ok(())
        }
    });
    let provider_order = order.clone();
    let provider = TestPlugin::new("provider", &["a"], &[]).cleanup(move || {
        let provider_order = provider_order.clone();
        async move {
            provider_order.push("provider");
            Ok(())
        }
    });
    let provider_mount = mount_plugin(&ctx, Arc::new(provider), ()).await.unwrap();
    let dependent_mount = mount_plugin(&ctx, Arc::new(dependent), ()).await.unwrap();

    let err = provider_mount.dispose().await.unwrap_err();
    assert_eq!(err.to_string(), "dependent cleanup failed");
    assert_eq!(attempts.get(), 1);
    assert!(order.items().is_empty());

    let err = dependent_mount.dispose().await.unwrap_err();
    assert_eq!(err.to_string(), "dependent cleanup failed");
    assert_eq!(attempts.get(), 2);
    assert!(order.items().is_empty());

    provider_mount.dispose().await.unwrap();
    assert_eq!(attempts.get(), 3);
    assert_eq!(order.items(), ["dependent", "provider"]);
    dependent_mount.dispose().await.unwrap();
}

#[tokio::test]
async fn cancelled_setup_failure_keeps_cleanup_running_until_release() {
    let ctx = agent(None);
    let (started, release, finished) = (Gate::new(), Gate::new(), Gate::new());
    let scope: Arc<Mutex<Option<Context>>> = Arc::default();
    let (s, r, f, slot) = (
        started.clone(),
        release.clone(),
        finished.clone(),
        scope.clone(),
    );
    let failing = ActionPlugin("cancelled", move |ctx: &Context| {
        *slot.lock().unwrap() = Some(ctx.clone());
        let (s, r, f) = (s.clone(), r.clone(), f.clone());
        ctx.effect(move || {
            Disposable::new(move || {
                let (s, r, f) = (s.clone(), r.clone(), f.clone());
                async move {
                    s.open();
                    r.wait().await;
                    f.open();
                    Ok(())
                }
            })
        })?;
        Err(KernelError::msg("setup failed"))
    });
    let host = ctx.clone();
    let use_task = tokio::spawn(async move { mount_plugin(&host, Arc::new(failing), ()).await });
    started.wait().await;
    use_task.abort();
    assert!(use_task.await.unwrap_err().is_cancelled());
    assert!(!finished.is_open());

    // Dropping the mount future must not abandon the shielded teardown.
    release.open();
    let scope = scope.lock().unwrap().clone().unwrap();
    scope.dispose().await.unwrap();
    assert!(finished.is_open());
    assert!(scope.is_disposed());
}

#[tokio::test]
async fn sibling_service_events_do_not_retry_other_agent_cleanup() {
    let parent = Context::new();
    let parent_dependency = parent.register_any("dep", object()).unwrap();
    let (first, second) = (agent(Some(&parent)), agent(Some(&parent)));
    let same_key = first.register_any("dep", object()).unwrap();
    let unrelated = first.register_any("unrelated", object()).unwrap();
    let attempts = Counter::default();
    let counter = attempts.clone();
    let plugin = TestPlugin::new("sibling-dependent", &[], &["dep"]).cleanup(move || {
        let counter = counter.clone();
        async move {
            if counter.bump() == 1 {
                return Err(KernelError::msg("dependent cleanup failed"));
            }
            Ok(())
        }
    });
    let mounted = mount_plugin(&second, Arc::new(plugin), ()).await.unwrap();
    same_key.dispose().await.unwrap();
    assert_eq!(attempts.get(), 0);

    let err = parent_dependency.dispose().await.unwrap_err();
    assert_eq!(err.to_string(), "dependent cleanup failed");
    assert_eq!(attempts.get(), 1);

    unrelated.dispose().await.unwrap();
    assert_eq!(attempts.get(), 1);
    mounted.dispose().await.unwrap();
    assert_eq!(attempts.get(), 2);
    first.dispose().await.unwrap();
    second.dispose().await.unwrap();
}

#[tokio::test]
async fn relevant_withdrawal_joins_a_busy_dependent_cleanup() {
    let ctx = agent(None);
    let trigger = ctx.register_any("trigger", object()).unwrap();
    let (started, release) = (Gate::new(), Gate::new());
    let order = Log::default();
    let (s, r, dep_order) = (started.clone(), release.clone(), order.clone());
    let dependent =
        TestPlugin::new("busy-dependent", &[], &["dep", "trigger"]).cleanup(move || {
            let (s, r, dep_order) = (s.clone(), r.clone(), dep_order.clone());
            async move {
                s.open();
                r.wait().await;
                dep_order.push("dependent");
                Ok(())
            }
        });
    let provider_order = order.clone();
    let provider = TestPlugin::new("busy-provider", &["dep"], &[]).cleanup(move || {
        let provider_order = provider_order.clone();
        async move {
            provider_order.push("provider");
            Ok(())
        }
    });
    let provider = mount_plugin(&ctx, Arc::new(provider), ()).await.unwrap();
    let dependent = mount_plugin(&ctx, Arc::new(dependent), ()).await.unwrap();
    let trigger_removal = tokio::spawn(async move { trigger.dispose().await });
    started.wait().await;
    let provider_removal = tokio::spawn(async move { provider.dispose().await });
    settle().await;
    assert!(order.items().is_empty());

    release.open();
    trigger_removal.await.unwrap().unwrap();
    provider_removal.await.unwrap().unwrap();
    assert_eq!(order.items(), ["dependent", "provider"]);
    dependent.dispose().await.unwrap();
}

#[tokio::test]
async fn requirement_returning_during_cleanup_reactivates_plugin() {
    let ctx = agent(None);
    let dependency = ctx.register_any("dep", object()).unwrap();
    let (started, release) = (Gate::new(), Gate::new());
    let activations = Counter::default();
    let (s, r, count) = (started.clone(), release.clone(), activations.clone());
    struct Rebinding<F>(F);
    impl<F> Plugin for Rebinding<F>
    where
        F: Fn(&Context) -> Result<Disposable, KernelError> + Send + Sync + 'static,
    {
        type Config = ();
        fn name(&self) -> &str {
            "rebinding"
        }
        fn version(&self) -> &str {
            "1"
        }
        fn provides(&self) -> Vec<String> {
            vec!["out".into()]
        }
        fn requires(&self) -> Vec<String> {
            vec!["dep".into()]
        }
        fn setup(&self, ctx: &Context, _config: &()) -> Result<Disposable, KernelError> {
            (self.0)(ctx)
        }
    }
    let plugin = Rebinding(move |ctx: &Context| {
        count.bump();
        let registration = ctx.register_any("out", object())?;
        let (s, r) = (s.clone(), r.clone());
        Ok(Disposable::new(move || {
            let (s, r, registration) = (s.clone(), r.clone(), registration.clone());
            async move {
                s.open();
                r.wait().await;
                registration.dispose().await
            }
        }))
    });
    let mounted = mount_plugin(&ctx, Arc::new(plugin), ()).await.unwrap();
    let removal = tokio::spawn(async move { dependency.dispose().await });
    started.wait().await;
    let replacement = ctx.register_any("dep", object()).unwrap();
    tokio::task::yield_now().await;
    release.open();
    removal.await.unwrap().unwrap();
    settle().await;

    assert_eq!(activations.get(), 2);
    assert!(ctx.get_any("out").is_some());
    replacement.dispose().await.unwrap();
    mounted.dispose().await.unwrap();
}

#[tokio::test]
async fn closing_context_does_not_reactivate_plugin() {
    let parent = Context::new();
    parent.register_any("x", object()).unwrap();
    let ctx = agent(Some(&parent));
    let dependent = Arc::new(TestPlugin::new("dependent", &["out"], &["x"]));
    let provider = Arc::new(TestPlugin::new("provider", &["x"], &[]));

    let mount = PluginMount::new(&ctx, dependent.clone(), ());
    mount.start().await.unwrap();
    mount_plugin(&ctx, provider, ()).await.unwrap();
    mount.reconcile().await.unwrap();
    assert_eq!(dependent.calls.get(), 2);

    ctx.dispose().await.unwrap();
    assert_eq!(dependent.calls.get(), 2);
}

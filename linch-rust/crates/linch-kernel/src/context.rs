//! Per-agent service container with a shared event bus and reversible child scopes.
//!
//! Port of `src/linch/kernel/context.py`. Services are keyed by name and stored
//! type-erased; [`ServiceKey`] gives a typed handle on one name.

use std::any::Any;
use std::cell::RefCell;
use std::collections::HashMap;
use std::marker::PhantomData;
use std::sync::{Arc, Mutex, Weak};

use crate::bus::{EmitPoint, EventBus, Next, Reply, SerialPoint, WaterfallPoint};
use crate::effects::{Disposable, EffectScope, IntoDisposers};
use crate::error::KernelError;

/// A type-erased service value.
pub type Service = Arc<dyn Any + Send + Sync>;

/// Published on [`CONTEXT_SERVICE`] after a service is added or removed.
#[derive(Clone)]
pub struct ServiceChange {
    /// The context whose registry changed.
    pub context: Context,
    /// The service key that changed.
    pub key: Arc<str>,
}

/// Lifecycle point fired after a context's service registry changes.
pub const CONTEXT_SERVICE: EmitPoint<ServiceChange> = EmitPoint::new("context/service");

thread_local! {
    // The activation scope of the plugin setup running on this thread. Setup is
    // synchronous, so this matches Python's ContextVar for its duration.
    static PLUGIN_SCOPE: RefCell<Option<Weak<Inner>>> = const { RefCell::new(None) };
}

/// A typed handle on one service name.
///
/// ```
/// use std::sync::Arc;
/// use linch_kernel::{Context, ServiceKey};
/// const GREETING: ServiceKey<String> = ServiceKey::new("greeting");
/// let ctx = Context::new();
/// let _undo = GREETING.register(&ctx, Arc::new("hi".to_string())).unwrap();
/// assert_eq!(GREETING.require(&ctx).unwrap().as_str(), "hi");
/// ```
pub struct ServiceKey<T> {
    name: &'static str,
    _type: PhantomData<fn() -> T>,
}

impl<T: Any + Send + Sync> ServiceKey<T> {
    /// Name a typed service key.
    pub const fn new(name: &'static str) -> Self {
        Self {
            name,
            _type: PhantomData,
        }
    }

    /// The service name.
    pub const fn name(&self) -> &'static str {
        self.name
    }

    /// See [`Context::register`].
    ///
    /// # Errors
    ///
    /// See [`Context::register_any`].
    pub fn register(&self, ctx: &Context, service: Arc<T>) -> Result<Disposable, KernelError> {
        ctx.register(self.name, service)
    }

    /// See [`Context::get`].
    pub fn get(&self, ctx: &Context) -> Option<Arc<T>> {
        ctx.get(self.name)
    }

    /// See [`Context::require`].
    ///
    /// # Errors
    ///
    /// See [`Context::require`].
    pub fn require(&self, ctx: &Context) -> Result<Arc<T>, KernelError> {
        ctx.require(self.name)
    }
}

/// Owns services, effects, and extension registrations for one agent tree.
///
/// Child contexts share the event bus (unless isolated) but keep service
/// registrations separate; lookups fall back to parents. Clones share the same
/// context. A parent keeps its children alive until they are disposed, so
/// always `dispose()` a context: dropping one leaks its subtree, the same way
/// dropping a [`Disposable`] leaks its registration.
#[derive(Clone)]
pub struct Context(Arc<Inner>);

/// A non-owning reference to a [`Context`].
#[derive(Clone)]
pub struct WeakContext(Weak<Inner>);

impl WeakContext {
    /// The context, if it is still alive.
    pub fn upgrade(&self) -> Option<Context> {
        self.0.upgrade().map(Context)
    }
}

pub(crate) struct Inner {
    label: String,
    events: EventBus,
    scope: EffectScope,
    teardown: Disposable,
    parent: Option<Context>,
    state: Mutex<State>,
}

#[derive(Default)]
pub(crate) struct State {
    pub(crate) services: HashMap<String, Service>,
    pub(crate) children: Vec<Context>,
    pub(crate) plugin_exports: HashMap<String, (Context, Context)>,
    pub(crate) before_dispose: Option<Disposable>,
    pub(crate) plugin_requires: Vec<String>,
}

impl Default for Context {
    fn default() -> Self {
        Self::new()
    }
}

impl Context {
    /// Create a root context labelled `"root"`.
    pub fn new() -> Self {
        Self::build("root".into(), EventBus::new(), None)
    }

    fn build(label: String, events: EventBus, parent: Option<Context>) -> Self {
        Self(Arc::new_cyclic(|weak: &Weak<Inner>| {
            let weak = weak.clone();
            Inner {
                label,
                events,
                scope: EffectScope::new(),
                teardown: Disposable::new(move || {
                    let ctx = weak.upgrade().map(Context);
                    async move {
                        match ctx {
                            Some(ctx) => ctx.run_dispose().await,
                            None => Ok(()),
                        }
                    }
                }),
                parent,
                state: Mutex::new(State::default()),
            }
        }))
    }

    /// Human-readable label used in diagnostics.
    pub fn label(&self) -> &str {
        &self.0.label
    }

    /// The event bus this context publishes on.
    pub fn events(&self) -> &EventBus {
        &self.0.events
    }

    /// A non-owning handle.
    pub fn downgrade(&self) -> WeakContext {
        WeakContext(Arc::downgrade(&self.0))
    }

    /// Whether two handles are the same context.
    pub fn ptr_eq(&self, other: &Context) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
    }

    /// Whether every owned cleanup has completed.
    pub fn is_disposed(&self) -> bool {
        self.0.scope.is_disposed()
    }

    /// Install a typed service. See [`Context::register_any`].
    ///
    /// # Errors
    ///
    /// See [`Context::register_any`].
    pub fn register<T: Any + Send + Sync>(
        &self,
        key: &str,
        service: Arc<T>,
    ) -> Result<Disposable, KernelError> {
        self.register_any(key, service)
    }

    /// Install a service and return the handle that removes exactly it.
    ///
    /// Removal publishes [`CONTEXT_SERVICE`], awaits every listener, and stays
    /// retryable if one fails.
    ///
    /// # Errors
    ///
    /// [`KernelError::Closed`] while disposing; [`KernelError::Config`] if this
    /// scope or a plugin export already claims `key`, or plugin setup tries to
    /// register outside its activation scope.
    pub fn register_any(&self, key: &str, service: Service) -> Result<Disposable, KernelError> {
        self.ensure_active()?;
        {
            let state = self.state();
            if state.services.contains_key(key) {
                return Err(KernelError::config(format!(
                    "service {key:?} already registered in this scope"
                )));
            }
            if state.plugin_exports.contains_key(key) {
                return Err(KernelError::config(format!(
                    "service {key:?} is already provided by a plugin"
                )));
            }
        }
        let key: Arc<str> = key.into();
        let disposer = self.0.scope.effect(|| {
            self.state()
                .services
                .insert(key.to_string(), service.clone());
            self.removal(key.clone(), service)
        })?;
        if self.events().has_listeners(CONTEXT_SERVICE.name()) && !in_plugin_setup() {
            self.events().emit(
                &CONTEXT_SERVICE,
                ServiceChange {
                    context: self.clone(),
                    key,
                },
            );
        }
        Ok(disposer)
    }

    fn removal(&self, key: Arc<str>, service: Service) -> Disposable {
        let (weak, pending) = (self.downgrade(), Arc::new(Mutex::new(None::<Disposable>)));
        Disposable::new(move || {
            let notification = {
                let mut pending = pending.lock().unwrap_or_else(|p| p.into_inner());
                if pending.is_none()
                    && let Some(ctx) = weak.upgrade()
                {
                    *pending = ctx.remove_service(&key, &service);
                }
                pending.clone()
            };
            async move {
                match notification {
                    Some(notification) => notification.dispose().await,
                    None => Ok(()),
                }
            }
        })
    }

    /// Remove `key` if it still holds `service`; return the change notification.
    fn remove_service(&self, key: &Arc<str>, service: &Service) -> Option<Disposable> {
        {
            let mut state = self.state();
            match state.services.get(key.as_ref()) {
                Some(current) if same(current, service) => state.services.remove(key.as_ref()),
                _ => return None,
            };
        }
        if !self.events().has_listeners(CONTEXT_SERVICE.name()) {
            return None;
        }
        let change = ServiceChange {
            context: self.clone(),
            key: key.clone(),
        };
        Some(self.events().notification(&CONTEXT_SERVICE, change))
    }

    /// Read a service, falling back through parent scopes.
    ///
    /// Returns `None` when nothing live resolves `key`.
    pub fn get_any(&self, key: &str) -> Option<Service> {
        let exported = {
            let state = self.state();
            if self.0.scope.is_closed() {
                None
            } else if let Some(service) = state.services.get(key) {
                return Some(service.clone());
            } else {
                state.plugin_exports.get(key).cloned()
            }
        };
        if let Some((owner, source)) = exported
            && !owner.scope_closed()
            && !source.scope_closed()
            && let Some(service) = source.state().services.get(key)
        {
            return Some(service.clone());
        }
        self.0
            .parent
            .as_ref()
            .and_then(|parent| parent.get_any(key))
    }

    /// Read a typed service; `None` if missing or of another type.
    pub fn get<T: Any + Send + Sync>(&self, key: &str) -> Option<Arc<T>> {
        self.get_any(key)
            .and_then(|service| service.downcast::<T>().ok())
    }

    /// Read a required typed service (Python's `ctx.<name>`).
    ///
    /// # Errors
    ///
    /// [`KernelError::MissingService`] or [`KernelError::ServiceType`].
    pub fn require<T: Any + Send + Sync>(&self, key: &str) -> Result<Arc<T>, KernelError> {
        let service = self
            .get_any(key)
            .ok_or_else(|| KernelError::MissingService {
                key: key.to_string(),
                label: self.label().to_string(),
            })?;
        service
            .downcast::<T>()
            .map_err(|_| KernelError::ServiceType {
                key: key.to_string(),
                expected: std::any::type_name::<T>(),
            })
    }

    /// Register an emit listener owned by this context.
    ///
    /// # Errors
    ///
    /// [`KernelError::Closed`] while disposing; [`KernelError::Config`] for a
    /// plugin-scope escape or a payload-type clash.
    pub fn on<A, F>(&self, point: &EmitPoint<A>, listener: F) -> Result<Disposable, KernelError>
    where
        A: 'static,
        F: Fn(A) -> Reply<()> + Send + Sync + 'static,
    {
        self.ensure_active()?;
        self.0.scope.add(self.events().on(point, listener)?)
    }

    /// Register a serial listener owned by this context.
    ///
    /// # Errors
    ///
    /// See [`Context::on`].
    pub fn on_serial<A, R, F>(
        &self,
        point: &SerialPoint<A, R>,
        listener: F,
    ) -> Result<Disposable, KernelError>
    where
        A: 'static,
        R: 'static,
        F: Fn(A) -> Reply<R> + Send + Sync + 'static,
    {
        self.ensure_active()?;
        self.0.scope.add(self.events().on_serial(point, listener)?)
    }

    /// Register a waterfall listener owned by this context.
    ///
    /// # Errors
    ///
    /// See [`Context::on`].
    pub fn on_waterfall<A, R, F>(
        &self,
        point: &WaterfallPoint<A, R>,
        listener: F,
    ) -> Result<Disposable, KernelError>
    where
        A: 'static,
        R: 'static,
        F: Fn(A, Next<R>) -> Reply<R> + Send + Sync + 'static,
    {
        self.ensure_active()?;
        self.0
            .scope
            .add(self.events().on_waterfall(point, listener)?)
    }

    /// Run and own a reversible effect.
    ///
    /// # Errors
    ///
    /// See [`Context::on`]; `setup` is not called on error.
    pub fn effect<F, R>(&self, setup: F) -> Result<Disposable, KernelError>
    where
        F: FnOnce() -> R,
        R: IntoDisposers,
    {
        self.ensure_active()?;
        self.0.scope.effect(setup)
    }

    /// Create an owned child with its own services and, optionally, its own bus.
    ///
    /// With `isolate_events`, listeners are snapshotted onto an independent bus;
    /// service changes in this context or its ancestors still reach the child.
    ///
    /// # Errors
    ///
    /// See [`Context::on`].
    pub fn scope(&self, label: &str, isolate_events: bool) -> Result<Context, KernelError> {
        self.ensure_active()?;
        let bus = if isolate_events {
            self.events().fork(&[CONTEXT_SERVICE.name()])
        } else {
            self.events().clone()
        };
        let child = Context::build(label.to_string(), bus.clone(), Some(self.clone()));
        self.state().children.push(child.clone());
        self.0.scope.add(child.0.teardown.clone())?;
        if isolate_events {
            let weak = child.downgrade();
            let relay = self
                .events()
                .on(&CONTEXT_SERVICE, move |change: ServiceChange| {
                    let Some(child) = weak.upgrade() else {
                        return Reply::Done;
                    };
                    if child.within(&change.context) && bus.has_listeners(CONTEXT_SERVICE.name()) {
                        let notification = bus.notification(&CONTEXT_SERVICE, change);
                        return Reply::later(async move { notification.dispose().await });
                    }
                    Reply::Done
                })?;
            child.0.scope.add(relay)?;
        }
        Ok(child)
    }

    /// Run synchronous plugin `setup` with this context as the activation scope.
    ///
    /// While it runs, every context outside this one rejects mutation.
    ///
    /// # Errors
    ///
    /// See [`Context::on`], plus whatever `setup` returns.
    pub fn setup_plugin<F>(&self, setup: F) -> Result<Disposable, KernelError>
    where
        F: FnOnce(&Context) -> Result<Disposable, KernelError>,
    {
        self.ensure_active()?;
        struct Restore(Option<Weak<Inner>>);
        impl Drop for Restore {
            fn drop(&mut self) {
                let previous = self.0.take();
                PLUGIN_SCOPE.with(|slot| *slot.borrow_mut() = previous);
            }
        }
        let previous = PLUGIN_SCOPE.with(|slot| slot.replace(Some(Arc::downgrade(&self.0))));
        let _restore = Restore(previous);
        setup(self)
    }

    /// Dispose this context and await all owned cleanup.
    ///
    /// Plugin exports are withdrawn first. A failed cleanup stays retryable.
    ///
    /// # Errors
    ///
    /// The collected cleanup failures.
    pub async fn dispose(&self) -> Result<(), KernelError> {
        self.0.teardown.dispose().await
    }

    async fn run_dispose(&self) -> Result<(), KernelError> {
        self.0.scope.close();
        let before = self.state().before_dispose.clone();
        if let Some(before) = before {
            before.dispose().await?;
        }
        self.0.scope.dispose().await?;
        if let Some(parent) = &self.0.parent {
            parent.state().children.retain(|child| !child.ptr_eq(self));
        }
        Ok(())
    }

    fn ensure_active(&self) -> Result<(), KernelError> {
        self.0.scope.ensure_active()?;
        let active = PLUGIN_SCOPE.with(|slot| slot.borrow().as_ref().and_then(Weak::upgrade));
        match active.map(Context) {
            Some(active) if !self.within(&active) => Err(KernelError::config(
                "plugin setup may only register effects in its activation scope",
            )),
            _ => Ok(()),
        }
    }

    /// Whether this context is `ancestor` or one of its descendants.
    pub(crate) fn within(&self, ancestor: &Context) -> bool {
        let mut current = Some(self);
        while let Some(ctx) = current {
            if ctx.ptr_eq(ancestor) {
                return true;
            }
            current = ctx.0.parent.as_ref();
        }
        false
    }

    pub(crate) fn parent(&self) -> Option<&Context> {
        self.0.parent.as_ref()
    }

    pub(crate) fn scope_closed(&self) -> bool {
        self.0.scope.is_closed()
    }

    pub(crate) fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.0
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

fn in_plugin_setup() -> bool {
    PLUGIN_SCOPE.with(|slot| slot.borrow().is_some())
}

/// Whether two services are the same allocation.
pub(crate) fn same(a: &Service, b: &Service) -> bool {
    std::ptr::addr_eq(Arc::as_ptr(a), Arc::as_ptr(b))
}

impl std::fmt::Debug for Context {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Context")
            .field("label", &self.label())
            .finish_non_exhaustive()
    }
}

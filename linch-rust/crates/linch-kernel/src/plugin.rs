//! Plugins: dormant-until-ready mounts that publish declared services.
//!
//! Port of `src/linch/kernel/plugins.py`. A mount listens for service changes
//! on its host, activates the plugin in a fresh child scope once every required
//! service resolves, and withdraws everything when a requirement disappears or
//! changes identity. Reconciliation is serialized with busy/pending flags.

use std::collections::HashSet;
use std::sync::{Arc, Mutex, Weak};

use crate::bus::Reply;
use crate::context::{CONTEXT_SERVICE, Context, Service, ServiceChange, same};
use crate::effects::{Disposable, EffectScope};
use crate::error::KernelError;

/// A plugin that can be mounted on a context.
pub trait Plugin: Send + Sync + 'static {
    /// Caller-supplied configuration passed to every activation.
    type Config: Send + Sync + 'static;

    /// Stable identifier used in diagnostics.
    fn name(&self) -> &str;
    /// Version reported by the implementation.
    fn version(&self) -> &str;
    /// Service keys the plugin may register during setup.
    fn provides(&self) -> Vec<String>;
    /// Service keys that must resolve before setup runs.
    fn requires(&self) -> Vec<String>;
    /// Configure the plugin in its fresh activation scope.
    ///
    /// # Errors
    ///
    /// Any failure; the activation scope is then rolled back.
    fn setup(&self, ctx: &Context, config: &Self::Config) -> Result<Disposable, KernelError>;
}

/// Mount `plugin` on `host` and return the handle that unmounts it.
///
/// # Errors
///
/// A failed first activation; the mount is rolled back.
pub async fn mount_plugin<P: Plugin>(
    host: &Context,
    plugin: Arc<P>,
    config: P::Config,
) -> Result<Disposable, KernelError> {
    PluginMount::new(host, plugin, config).start().await
}

/// One plugin mounted on one host context.
pub struct PluginMount<P: Plugin>(Arc<Mount<P>>);

impl<P: Plugin> Clone for PluginMount<P> {
    fn clone(&self) -> Self {
        Self(self.0.clone())
    }
}

struct Mount<P: Plugin> {
    host: Context,
    plugin: Arc<P>,
    config: P::Config,
    provides: Vec<String>,
    requires: Vec<String>,
    state: Mutex<MountState>,
}

#[derive(Default)]
struct MountState {
    active: Option<(Context, Vec<Service>)>,
    pending_cleanup: Option<Context>,
    busy: bool,
    pending: bool,
    closed: bool,
    listener: Option<Disposable>,
}

impl<P: Plugin> PluginMount<P> {
    /// Prepare a mount without starting it.
    pub fn new(host: &Context, plugin: Arc<P>, config: P::Config) -> Self {
        Self(Arc::new(Mount {
            host: host.clone(),
            provides: plugin.provides(),
            requires: plugin.requires(),
            plugin,
            config,
            state: Mutex::new(MountState::default()),
        }))
    }

    /// Start listening and activate now if every requirement resolves.
    ///
    /// # Errors
    ///
    /// A failed first activation, after the mount has been torn down.
    pub async fn start(&self) -> Result<Disposable, KernelError> {
        let weak: Weak<Mount<P>> = Arc::downgrade(&self.0);
        let listener = self
            .0
            .host
            .on(&CONTEXT_SERVICE, move |change: ServiceChange| {
                match weak.upgrade() {
                    Some(mount) => Reply::later(PluginMount(mount).reconcile_on(Some(change))),
                    None => Reply::Done,
                }
            })?;
        self.state().listener = Some(listener);
        let mount = self.clone();
        let handle = self
            .0
            .host
            .effect(move || Disposable::new(move || mount.clone().unmount()))?;
        if let Err(error) = self.reconcile().await {
            handle.dispose().await?;
            return Err(error);
        }
        Ok(handle)
    }

    /// Re-evaluate requirements and (de)activate as needed.
    ///
    /// # Errors
    ///
    /// A failed activation or cleanup.
    pub async fn reconcile(&self) -> Result<(), KernelError> {
        self.clone().reconcile_on(None).await
    }

    async fn reconcile_on(self, change: Option<ServiceChange>) -> Result<(), KernelError> {
        if let Some(change) = &change
            && !self.change_relevant(change)
        {
            return Ok(());
        }
        let joined = {
            let mut state = self.state();
            if state.busy {
                state.pending = true;
                Some(state.pending_cleanup.clone())
            } else {
                state.busy = true;
                None
            }
        };
        if let Some(cleanup) = joined {
            if let Some(scope) = cleanup {
                self.finish_deactivation(&scope).await?;
            }
            return Ok(());
        }
        struct Idle<'a>(&'a Mutex<MountState>);
        impl Drop for Idle<'_> {
            fn drop(&mut self) {
                self.0.lock().unwrap_or_else(|p| p.into_inner()).busy = false;
            }
        }
        let _idle = Idle(&self.0.state);
        self.reconcile_loop().await
    }

    async fn reconcile_loop(&self) -> Result<(), KernelError> {
        loop {
            let cleanup = {
                let mut state = self.state();
                state.pending = false;
                state.pending_cleanup.clone()
            };
            if let Some(scope) = cleanup {
                self.finish_deactivation(&scope).await?;
            }
            let found: Vec<Option<Service>> = self
                .0
                .requires
                .iter()
                .map(|key| self.0.host.get_any(key))
                .collect();
            let ready = found.iter().all(Option::is_some);
            let (is_active, changed, closed) = {
                let state = self.state();
                let changed = state.active.as_ref().is_some_and(|(_, old)| {
                    !old.iter()
                        .zip(&found)
                        .all(|(old, new)| new.as_ref().is_some_and(|new| same(old, new)))
                });
                (state.active.is_some(), changed, state.closed)
            };
            if ready && !is_active && !closed && !self.0.host.scope_closed() {
                self.activate(found.into_iter().flatten().collect()).await?;
            } else if (!ready || changed) && is_active {
                let scope = {
                    let mut state = self.state();
                    let scope = state.active.take().map(|(scope, _)| scope);
                    state.pending_cleanup = scope.clone();
                    if ready && !state.closed {
                        state.pending = true;
                    }
                    scope
                };
                if let Some(scope) = scope {
                    self.finish_deactivation(&scope).await?;
                }
            }
            let state = self.state();
            if state.closed || !state.pending {
                return Ok(());
            }
        }
    }

    fn change_relevant(&self, change: &ServiceChange) -> bool {
        let key = change.key.as_ref();
        if !self.0.requires.iter().any(|r| r == key) {
            return false;
        }
        let cleanup = self.state().pending_cleanup.clone();
        if let Some(cleanup) = cleanup
            && change.context.within(&cleanup)
        {
            return false;
        }
        let mut current = Some(self.0.host.clone());
        while let Some(ctx) = current.take() {
            if ctx.ptr_eq(&change.context) {
                return true;
            }
            let (local, exported) = {
                let state = ctx.state();
                (
                    state.services.contains_key(key),
                    state.plugin_exports.get(key).cloned(),
                )
            };
            if local {
                return false;
            }
            if let Some((owner, source)) = exported {
                if change.context.within(&source) {
                    return true;
                }
                let live = !owner.scope_closed() && !source.scope_closed();
                if live && source.state().services.contains_key(key) {
                    return false;
                }
            }
            current = ctx.parent().cloned();
        }
        false
    }

    fn check_cycle(&self) -> Result<(), KernelError> {
        let (mut pending, mut seen) = (self.0.requires.clone(), HashSet::new());
        while let Some(key) = pending.pop() {
            if self.0.provides.contains(&key) {
                return Err(KernelError::config(format!(
                    "plugin dependency cycle at {key:?}"
                )));
            }
            if !seen.insert(key.clone()) {
                continue;
            }
            let exported = self.0.host.state().plugin_exports.get(&key).cloned();
            if let Some((owner, _)) = exported {
                pending.extend(owner.state().plugin_requires.clone());
            }
        }
        Ok(())
    }

    async fn activate(&self, requirements: Vec<Service>) -> Result<(), KernelError> {
        self.check_cycle()?;
        let scope = self
            .0
            .host
            .scope(&format!("plugin:{}", self.0.plugin.name()), false)?;
        let Err(error) = self.install(&scope, requirements) else {
            return Ok(());
        };
        self.state().pending_cleanup = Some(scope.clone());
        match self.finish_deactivation(&scope).await {
            Ok(()) => Err(error),
            Err(cleanup) => Err(KernelError::Cleanup(vec![error, cleanup])),
        }
    }

    fn install(&self, scope: &Context, requirements: Vec<Service>) -> Result<(), KernelError> {
        let name = self.0.plugin.name();
        let result = scope.setup_plugin(|ctx| self.0.plugin.setup(ctx, &self.0.config))?;
        scope.effect(|| result)?;
        let services = plugin_services(scope);
        if let Some((key, _)) = services
            .iter()
            .find(|(key, _)| !self.0.provides.contains(key))
        {
            return Err(KernelError::config(format!(
                "{name:?}: service {key:?} not in provides"
            )));
        }
        let keys: Vec<String> = services.iter().map(|(key, _)| key.clone()).collect();
        if keys.iter().collect::<HashSet<_>>().len() != keys.len() {
            return Err(KernelError::config(format!(
                "plugin {name:?} registered duplicate service keys"
            )));
        }
        scope.state().plugin_requires = self.0.requires.clone();
        let (host, owner, notifications) =
            (self.0.host.clone(), scope.downgrade(), EffectScope::new());
        let withdraw = Disposable::new(move || {
            let (host, owner, notifications, keys) = (
                host.clone(),
                owner.clone(),
                notifications.clone(),
                keys.clone(),
            );
            async move {
                if let (false, Some(owner)) = (notifications.is_closed(), owner.upgrade()) {
                    for key in keys.iter().rev() {
                        if let Some(notification) = withdraw_plugin(&host, &owner, key) {
                            notifications.add(notification)?;
                        }
                    }
                }
                notifications.dispose().await
            }
        });
        scope.state().before_dispose = Some(withdraw);
        for (key, source) in &services {
            publish_plugin(&self.0.host, scope, key, source)?;
        }
        self.state().active = Some((scope.clone(), requirements));
        Ok(())
    }

    async fn finish_deactivation(&self, scope: &Context) -> Result<(), KernelError> {
        scope.dispose().await?;
        let mut state = self.state();
        if state
            .pending_cleanup
            .as_ref()
            .is_some_and(|pending| pending.ptr_eq(scope))
        {
            state.pending_cleanup = None;
        }
        Ok(())
    }

    async fn unmount(self) -> Result<(), KernelError> {
        let cleanup = {
            let mut state = self.state();
            state.closed = true;
            state.pending_cleanup.clone()
        };
        if let Some(scope) = cleanup {
            self.finish_deactivation(&scope).await?;
        }
        let active = {
            let mut state = self.state();
            let scope = state.active.take().map(|(scope, _)| scope);
            if scope.is_some() {
                state.pending_cleanup = scope.clone();
            }
            scope
        };
        if let Some(scope) = active {
            self.finish_deactivation(&scope).await?;
        }
        let listener = self.state().listener.clone();
        match listener {
            Some(listener) => listener.dispose().await,
            None => Ok(()),
        }
    }

    fn state(&self) -> std::sync::MutexGuard<'_, MountState> {
        self.0
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

/// Every service registered in `scope` or its descendants, with its source.
fn plugin_services(scope: &Context) -> Vec<(String, Context)> {
    let (keys, children) = {
        let state = scope.state();
        (
            state.services.keys().cloned().collect::<Vec<_>>(),
            state.children.clone(),
        )
    };
    let mut result: Vec<(String, Context)> =
        keys.into_iter().map(|key| (key, scope.clone())).collect();
    for child in &children {
        result.extend(plugin_services(child));
    }
    result
}

fn publish_plugin(
    host: &Context,
    owner: &Context,
    key: &str,
    source: &Context,
) -> Result<(), KernelError> {
    {
        let mut state = host.state();
        if state.services.contains_key(key) {
            return Err(KernelError::config(format!(
                "service {key:?} already exists in the mounting context"
            )));
        }
        if let Some((existing, _)) = state.plugin_exports.get(key)
            && !existing.ptr_eq(owner)
        {
            return Err(KernelError::config(format!(
                "service {key:?} is already provided by another plugin"
            )));
        }
        state
            .plugin_exports
            .insert(key.to_string(), (owner.clone(), source.clone()));
    }
    if host.events().has_listeners(CONTEXT_SERVICE.name()) {
        host.events().emit(
            &CONTEXT_SERVICE,
            ServiceChange {
                context: host.clone(),
                key: key.into(),
            },
        );
    }
    Ok(())
}

fn withdraw_plugin(host: &Context, owner: &Context, key: &str) -> Option<Disposable> {
    {
        let mut state = host.state();
        match state.plugin_exports.get(key) {
            Some((existing, _)) if existing.ptr_eq(owner) => state.plugin_exports.remove(key),
            _ => return None,
        };
    }
    host.events()
        .has_listeners(CONTEXT_SERVICE.name())
        .then(|| {
            host.events().notification(
                &CONTEXT_SERVICE,
                ServiceChange {
                    context: host.clone(),
                    key: key.into(),
                },
            )
        })
}

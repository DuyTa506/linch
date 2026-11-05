//! The Linch kernel: reversible effects, a typed event bus, per-agent scopes,
//! and plugins.
//!
//! Every registration returns a [`Disposable`] that undoes exactly that
//! registration. Each agent owns a [`Context`], so many agents can run in one
//! process without shared mutable state. Port of `src/linch/kernel/`.
#![forbid(unsafe_code)]

mod bus;
mod context;
mod effects;
mod error;
mod plugin;

pub use bus::{DispatchMode, EmitPoint, EventBus, Next, Reply, SerialPoint, WaterfallPoint};
pub use context::{CONTEXT_SERVICE, Context, Service, ServiceChange, ServiceKey, WeakContext};
pub use effects::{Disposable, EffectScope, IntoDisposers};
pub use error::{BoxError, KernelError};
pub use plugin::{Plugin, PluginMount, mount_plugin};

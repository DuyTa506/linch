//! Python-free core of the Linch agent kernel: shared types, wire codecs, the
//! event vocabulary, and the append-only session log.
//!
//! The wire format is JSON. Every type rejects unknown fields and unknown
//! `type` tags when decoding, so a reader never silently drops data. The one
//! exception is an event its writer marked ignorable, which is kept as raw
//! JSON in [`IgnorableEvent`].
//!
//! The crate root re-exports each module's entry types (the ones a caller
//! names directly: [`Event`], [`ProviderRequest`], [`SessionLog`], ...).
//! Payload structs that only ever appear *through* an entry type's enum
//! variant stay addressed under their own module instead
//! (`events::kinds::*` behind [`Event`], `provider::*Event` behind
//! [`ProviderEvent`]), so the root namespace doesn't carry every variant's
//! struct as a second, redundant path to the same type.

#![forbid(unsafe_code)]

pub mod agent;
pub mod canonical;
pub mod compaction;
pub mod events;
pub mod hooks;
pub mod ids;
pub mod journal;
pub mod json;
pub mod message;
pub mod permissions;
pub mod policy;
pub mod provider;
pub mod registration;
pub mod registry;
pub mod run;
pub mod runner;
pub mod scheduler;
pub mod session_log;
pub mod skills;
pub mod store;
pub mod tool;
pub mod usage;

pub use agent::{Agent, AgentBuilder, AgentSpec, BuildError, RunError, RunOptions};
pub use canonical::{
    CanonicalError, Digest, JOURNAL_MAX_DEPTH, MAX_ENCODE_DEPTH, check_depth, digest,
    digest_with_domain, to_canonical_string,
};
pub use compaction::CompactionSpec;
pub use events::{
    Envelope, Event, EventError, IgnorableEvent, Persistence, SubagentEvent, WIRE_VERSION,
};
pub use hooks::{
    CallbackError, ChainResult, Hook, HookChain, HookError, HookRecord, HookStatus, Hooks, Point,
    ToolMatcher,
};
pub use ids::{RunId, SessionId};
pub use journal::{Journal, JournalError, ReconcileError, Recovery, ToolRecovery, reconcile};
pub use json::{JsonObject, JsonValue};
pub use message::{
    CatalogChangeBlock, ContentBlock, ImageBlock, ImageSource, Message, RedactedThinkingBlock,
    Role, SystemBlock, TextBlock, ThinkingBlock, ToolResultBlock, ToolResultPart, ToolUseBlock,
};
pub use permissions::{
    PermissionCall, PermissionEngine, PermissionLayer, PermissionMode, PermissionPolicy,
    PolicyError, permission_decision_key,
};
pub use policy::{FailMode, HookPoint, PermissionDecision, Principal, PrincipalError};
pub use provider::scripted::{ScriptedFixture, ScriptedProvider, ScriptedTurn};
pub use provider::{
    Assembler, AssistantAssembly, CacheControl, CacheTtl, OutputSchema, ProviderEvent,
    ProviderRequest, ReasoningEffort, StreamError, Thinking, ToolChoice, ToolDefinition, assemble,
};
pub use provider::{Provider, ProviderError, ProviderStream};
pub use registration::Registration;
pub use registry::{
    OutputContractError, RegisteredTool, RegistryError, SchemaKind, ToolRegistry, ToolSnapshot,
};
pub use run::{Checkpoint, Phase, RunStatus, StopReason, ToolDecisionRecord};
pub use runner::RunStream;
pub use scheduler::{
    BatchPlan, CallOutcome, ExecuteOptions, PlannedAction, PlannedCall, RecordedApproval,
    RunIdentity, ToolSink,
};
pub use session_log::{LogEntry, MessageEntry, ProjectionEntry, SessionLog};
pub use skills::Skill;
pub use store::memory::{MemoryRunStore, MemorySessionStore};
pub use store::{Commit, Lease, NewRun, RunRecord, RunStore, SessionStore, StoreError};
pub use tool::{
    Tool, ToolContext, ToolOutput, ToolOutputError, ToolProgress, ToolResult, ToolScope,
};
pub use usage::Usage;

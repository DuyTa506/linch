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
pub mod ask_user;
pub mod budget;
pub mod canonical;
pub(crate) mod catalog;
pub mod compaction;
pub mod context_budget;
pub mod coordination;
pub mod events;
pub mod gates;
pub mod hooks;
pub mod ids;
pub mod journal;
pub mod json;
pub mod loop_guard;
pub mod memory;
pub mod message;
pub mod permissions;
pub mod policy;
pub mod provider;
pub mod redaction;
pub mod registration;
pub mod registry;
pub mod reliability;
pub mod run;
pub mod runner;
pub mod scheduler;
pub mod session_log;
pub mod skills;
pub mod store;
mod structured;
pub mod subagents;
pub mod system;
pub mod tool;
pub mod tool_cache;
pub mod tool_retry;
pub mod tool_search;
pub mod truncation;
pub mod usage;
pub mod verification;
pub mod vfs;

pub use agent::{Agent, AgentBuilder, AgentSpec, BuildError, ForkError, RunError, RunOptions};
pub use budget::RunBudget;
pub use canonical::{
    CanonicalError, Digest, JOURNAL_MAX_DEPTH, MAX_ENCODE_DEPTH, check_depth, digest,
    digest_with_domain, to_canonical_string,
};
pub use compaction::CompactionSpec;
pub use context_budget::ContextBudget;
pub use coordination::{Correlator, InMemoryMailbox, Mailbox, MailboxError, MailboxMessage};
pub use events::{
    Envelope, Event, EventError, IgnorableEvent, Persistence, SubagentEvent, WIRE_VERSION,
};
pub use gates::{CompletionGate, Prerequisite, ReliabilityPolicy};
pub use hooks::{
    CallbackError, ChainResult, Hook, HookChain, HookError, HookRecord, HookStatus, Hooks, Point,
    ToolMatcher,
};
pub use ids::{RunId, SessionId};
pub use journal::{Journal, JournalError, ReconcileError, Recovery, ToolRecovery, reconcile};
pub use json::{JsonObject, JsonValue};
pub use loop_guard::LoopGuard;
pub use memory::{
    InMemoryMemoryStore, InvalidMemory, Memory, MemoryError, MemoryStore, NewMemory,
    default_scope_cap, principal_scope, recall_terms, search_terms,
};
pub use message::{
    CatalogChangeBlock, ContentBlock, ImageBlock, ImageSource, Message, RedactedThinkingBlock,
    Role, SkillStamp, SystemBlock, TextBlock, ThinkingBlock, ToolResultBlock, ToolResultPart,
    ToolUseBlock,
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
pub use provider::{
    ContextSource, ModelInfo, ModelList, ModelsError, OutputSchemaMode, PromptCache,
    ProviderCapabilities, Support, ThinkingCaps, ThinkingState, ToolChoiceCaps,
};
pub use provider::{DEFAULT_CONTEXT_WINDOW, Provider, ProviderError, ProviderStream};
pub use provider::{ProviderGate, ProviderPermit, WeakProviderGate};
pub use registration::Registration;
pub use registry::{
    OutputContractError, RegisteredTool, RegistryError, SchemaKind, ToolRegistry, ToolSnapshot,
};
pub use reliability::ProviderRetry;
pub use run::{Checkpoint, Ending, Phase, RunStatus, StopReason, ToolDecisionRecord};
pub use runner::RunStream;
pub use scheduler::{
    BatchPlan, CallOutcome, ExecuteOptions, PlannedAction, PlannedCall, RecordedApproval, Retrying,
    RunIdentity, ToolSink,
};
pub use session_log::{LogEntry, MessageEntry, PrefixEntry, ProjectionEntry, SessionLog};
pub use skills::Skill;
pub use store::memory::{MemoryRunStore, MemorySessionStore};
pub use store::{
    Commit, Lease, NewRun, NewTask, RunRecord, RunStore, SessionStore, StoreError, Task, TaskPatch,
    TaskStatus, TaskStore,
};
pub use subagents::{SubagentDefinition, Workers};
pub use system::{SystemConfig, SystemSection};
pub use tool::{
    Tool, ToolContext, ToolOutput, ToolOutputError, ToolProgress, ToolResult, ToolScope,
};
pub use tool_cache::{
    ByteQuota, CacheScope, InMemoryToolCache, Lookup, PutLimits, ToolCache, ToolCacheError,
    ToolCacheHandle, ToolCacheStore,
};
pub use tool_retry::ToolRetry;
pub use tool_search::ToolSearch;
pub use truncation::TruncationRecovery;
pub use usage::Usage;

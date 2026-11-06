# Tools

> Part of the [linch-rust architecture guide](./README.md).

This page covers the tool contract in `linch-core` (the `Tool` trait, scopes,
the registry and how a batch runs) and the tools in `linch-tools`: the
workspace tools, the virtual filesystem with result offload, and the session
task tools. Use it when you write a tool, change how calls are scheduled, or
touch file confinement.

Python Linch's [tool protocol](../../../docs/architecture/tool-protocol.md)
describes the same ideas. What differs in Rust:

- A tool is any type that implements the `Tool` trait; there is no base
  struct and no `@tool` decorator. The trait's required methods are the
  attributes Python reads by duck typing.
- `scope()` has no default. In Linch 3.x a defaulted `read` scope let
  side-effecting functions run unapproved and in parallel (issue #99,
  `crates/linch-core/src/tool.rs:4-7`).
- A result is always `Result<ToolOutput, ToolOutputError>`. There is no
  legacy `ToolResult` object; `output_schema` is optional.

## The tool trait

`Tool` (`crates/linch-core/src/tool.rs:213`) is `Send + Sync` and stateless
per call; per-call data arrives in `ToolContext`.

| Method | Default | Contract |
|---|---|---|
| `name()` | required | 1 to 64 ASCII letters, digits, `_` or `-`: what Anthropic, OpenAI and Gemini all accept (`crates/linch-core/src/registry.rs:278-281`) |
| `description()` | required | Shown to the model |
| `input_schema()` | required | JSON Schema draft 2020-12 with `"type": "object"` at the top |
| `scope()` | required | `Read`, `Write` or `Exec` |
| `output_schema()` | `None` | When set, a successful value must satisfy it |
| `validate(input)` | identity | Checks beyond the schema; runs after schema validation and again after a `pre_tool_use` hook rewrites the input |
| `parallel(input)` | `scope().is_parallel_safe()` | Whether this call may run alongside other read calls |
| `summarize(input)` | the name | One line for events and approval prompts |
| `execute(input, ctx)` | required | Run on validated input; return promptly once `ctx.cancel` fires |

`ToolOutput` holds the `value` the model sees and `metadata` it does not.
`ToolOutputError` holds the `message` the model sees, an optional `code` for
the host, and optional `details` (`tool.rs:42-83`).

`ToolContext` (`tool.rs:117`) carries the session id, run id, `tool_use_id`,
the caller's `principal`, a `cancel` token (cancelled on run abort or when the
call ends) and the call's `deadline` when the agent sets a call timeout.
`idempotency_key()` is `run_id:tool_use_id` (`tool.rs:177`). A resumed run
never re-runs a call whose start was durable but whose end was not: it reports
it as interrupted. `report_progress` is best effort and never fails the tool
(`tool.rs:182`).

### Scopes and approval

| Scope | Default permission mode | Runs in parallel |
|---|---|---|
| `Read` | allowed | yes, with neighboring read calls |
| `Write` | asks | no |
| `Exec` | asks | no |

The scope decides auto-approval (`crates/linch-core/src/permissions/engine.rs:320`)
and, through the default `parallel`, concurrency. It is also part of the run
contract digest, next to the schema (`registry.rs:216-218`).

## The registry

`ToolRegistry` (`registry.rs:72`) is shared across runs behind an `Arc`.

- `register(tool)` (`registry.rs:86`) adds a tool under a free name. It fails
  on an invalid name, a duplicate, a top-level schema that is not an object,
  or a schema that is not valid draft 2020-12 or has a `$ref` that needs I/O.
  Schemas are compiled once here.
- `replace(tool)` (`registry.rs:95`) hides any tool of the same name. The
  replacement keeps the old position in `definitions()`.
- Every change to the visible set bumps `generation()` (`registry.rs:100`).
- `snapshot()` (`registry.rs:107`) returns an immutable `ToolSnapshot`. Each
  turn takes one and uses it throughout, so a registration that lands
  mid-turn never changes what that turn sees.
- `definitions()` (`registry.rs:209`) lists tools in first-registration order.
  The order is stable across snapshots because the prompt cache depends on it.

### Reversible registration

Every registration returns a `Registration` (`crates/linch-core/src/registration.rs:19`).
`dispose()` undoes exactly that registration and is idempotent; dropping the
handle undoes nothing (`registration.rs:1-6`). Disposing a tool that a later
`replace` hides changes nothing visible. Disposing the visible one brings back
the most recent live registration of that name, or removes the name. The
same handle type comes back from `HookChain::register`
(`crates/linch-core/src/hooks/chain.rs:132`) and from the MCP client, so a
host undoes a whole wiring by disposing the handles it kept.

### Catalog changes mid-session

A session's first prompt is logged with a `PrefixEntry`: the system blocks,
the tools and the listed skills at that moment
(`crates/linch-core/src/catalog.rs`). Every later request of the session
starts with exactly those bytes, so the prompt cache keeps them whatever the
registry, the skills or the subagents do. A change goes at the tail instead:
a `CatalogChangeBlock` (Python Linch's `<system-reminder>` text) in the next
user message the run logs anyway, so it costs no write.

- A removed tool keeps its row; a call to it is refused as not registered.
- An added tool is appended to the request's tools, which costs the cache
  once. `native_catalog_changes` on `AnthropicOptions` or `ResponsesOptions`
  sends it at the notice instead (off by default, not verified live). The
  request names those rows in `ProviderRequest::appended_tools`, so the
  adapter keeps them out of `tools` across a compaction too.
- A new description is told in text; a new input schema replaces its row.
- A compaction never rewrites the prefix; its summary restates the changes.

`Agent::reload_skills` and `Agent::reload_subagents` (and
`OpenedAgent::reload_*`, which rescan the config directory for a kind the
config turned on, and otherwise only re-apply a deep agent's roster and the
built-in skills) are reversible registrations of the same kind. The run contract stays the one the agent was
built with.

`AgentBuilder::tool_search(ToolSearch)` defers a large catalog
(`crates/linch-core/src/tool_search.rs`): past `max_tools` a new session is
offered only the tools whose `always_load` says so and `tool_search`. What a
search finds is told as added tools at the tail and offered from the next
request on; a call of a tool not offered is refused as deferred.

## How a batch runs

The tool calls of one response go through two steps
(`crates/linch-core/src/scheduler.rs:1-22`), so a run can suspend between
them:

1. **Plan.** For each call: resolve the name in the turn's snapshot, validate
   the input, run the `pre_tool_use` chain, validate the final input again,
   evaluate permissions (and the `permission_request` chain on `Ask`), then
   apply any recorded approval from an earlier suspend. A call that still
   needs approval suspends the run.
2. **Execute** (`crates/linch-core/src/scheduler/execute.rs:119`). Results
   come back one per call, in call order. A success is checked against the
   tool's `output_schema`, then the `post_tool_use` chain runs on the result.
   A tool that panics gets an error result naming the panic.

### Only read work runs in parallel

`execute` walks the calls in order and groups them
(`execute.rs:137-151`). A call joins the current group only if the group
started with a read call, the group is below the limit, and this call is a
read call too (`in_read_group`, `execute.rs:157`). A call that runs no tool
(refused, served from a hook, aborted) also joins. Any other call runs alone,
so the next call waits for it.

A read call is one whose tool's `parallel(input)` returns true. The default
returns true only for `Read` scope. A tool may return true for a call that does
only read work whatever its scope, for example a subagent whose tools all read
(`tool.rs:237-244`). Never return true for a call that writes or runs
anything.

The limit is `AgentSpec::max_tool_concurrency`, 4 by default; 0 counts as 1
(`crates/linch-core/src/agent/spec.rs:47-49`). `call_timeout_ms` bounds each
call: at the deadline the scheduler drops the call's future and reports a
timeout error.

### Durability around a tool

A tool that really runs costs two run-store writes: its start is made durable
before it runs, and its end right after it returns
(`crates/linch-core/src/runner/sink.rs:1-12`). A call that runs nothing has
its start and end staged and written with the next barrier. See
[stores](./stores.md#group-commit-write-counts) for the count per turn.

## Workspace tools

`workspace_tools(fs, reads)` (`crates/linch-tools/src/lib.rs:51`) returns
`Read`, `Write`, `Edit`, `Grep`, `Glob` and `Bash` over one `LocalFs`, sharing
one `ReadTracker`. The crate is Unix-only because Bash uses process groups.

| Tool | Scope | Behavior |
|---|---|---|
| `Read` | read | UTF-8 text with numbered lines; `offset`/`limit`, default 2000 lines; files over 10 MiB are refused (`crates/linch-tools/src/read.rs:14-17`) |
| `Write` | write | Replaces a whole file atomically and creates parent directories. An existing file must have been read in full at its current stamp |
| `Edit` | write | Replaces exact text, once unless `replace_all`. The file must have been read at its current stamp, including every line a match touches |
| `Grep` | read | ripgrep's engine in process; modes `files_with_matches`, `count`, `content`; default 250 entries, output capped at 30 000 bytes (`crates/linch-tools/src/grep.rs:15-25`) |
| `Glob` | read | ripgrep glob rules, newest first, at most 1000 paths (`crates/linch-tools/src/glob.rs:13`) |
| `Bash` | exec | `/bin/sh -c` in the root; see below |

Grep and Glob skip ignored files and do not follow symlinks; Grep also skips
hidden and binary files, and Glob lists hidden files only when the pattern
names a dot segment (`crates/linch-tools/src/fs.rs:232-248`).

### Read before write

`ReadTracker` (`crates/linch-tools/src/tracker.rs:24`) records, per session and
workspace path, the file's stamp (modification time and size) and the lines a
`Read` covered. A stamp change, by anyone, voids the evidence. A refused write
returns code `read_before_write` (`crates/linch-tools/src/input.rs:13`). The
evidence is in memory: a restarted process must read again before writing,
which is the safe direction. The host calls `ReadTracker::forget` when a
session ends (`tracker.rs:65`).

### Confinement

Every file tool goes through the `ExecutionFs` trait (`fs.rs:209`), so a
remote execution world can replace `LocalFs` without changing the tools.
`LocalFs` (`crates/linch-tools/src/fs/local.rs:26`) canonicalizes the root
once; `new` is blocking, so call it before the runtime starts or from a
blocking task. Each path is resolved by canonicalizing its longest existing
prefix; the rest may only hold plain names (`local.rs:68-72`). `..`, absolute
paths outside the root and symlinks that lead out are refused with
`FsError::Escape`. Paths returned are workspace paths: relative, `/`-separated,
symlinks resolved, so one file has one name. The check guards path arguments;
it does not stop a concurrent process from swapping a symlink between check
and use. Blocking file work runs on Tokio's blocking pool.

### Bash

`Bash` (`crates/linch-tools/src/bash.rs:48`) is not confined by `LocalFs`: it
starts in the root, but the command can reach anything the process user can.
Use the permission engine (it asks for `exec` by default) or a sandboxed
execution world when that matters. What it does guarantee:

- stdin is closed and the command leads a new process group
  (`crates/linch-tools/src/bash/process.rs:66`);
- the whole group gets `SIGKILL` on timeout, on cancel, when the call's future
  is dropped, and when the shell exits, so background jobs never outlive the
  call (`process.rs:135-148`);
- only nine host variables pass through (`PATH`, `HOME`, `TERM`, `LANG`,
  `LC_ALL`, `LC_CTYPE`, `USER`, `SHELL`, `TMPDIR`), so host credentials stay
  out of the environment (`bash.rs:33`); `with_env` adds more (`bash.rs:82`);
- the timeout defaults to 120 s and is capped at 30 min (`bash.rs:22-25`);
  with a call deadline it ends 100 ms before it, so the output so far reaches
  the model instead of a generic timeout (`bash.rs:140`);
- each stream keeps its first 4000 and last 26 000 bytes
  (`bash/capture.rs:5-8`);
- any exit status but 0 is an error result carrying the same text, with code
  `exit_status`, `timeout` or `aborted` (`bash.rs:151`).

## Virtual filesystem and result offload

The virtual filesystem is separate from the workspace: scratch notes and
offloaded results live wherever the backend keeps them. The contract,
`FileBackend` (`crates/linch-core/src/vfs.rs:83`), is in the core so a store
crate can implement it without the tools. It stores UTF-8 text by `/`-rooted
path with `read`, `write`, `edit`, `ls`, `exists` and `delete`. Every backend
normalizes paths with `normalize_path` (`vfs.rs:120`): `a//b/` and `/a/b` are
one file, `..` and NUL are refused, and the root is not a file. `edit` is
atomic per backend and uses `apply_edit` (`vfs.rs:163`). Every backend passes
`vfs::conformance::backend_suite`.

A file holds at most `MAX_FILE_BYTES` (10 MiB, `vfs.rs:25`), the workspace
`Read` and `Edit` limit. `apply_edit` refuses an edit whose result would pass
it, with `VfsError::TooLarge`, from the match count before it builds the
result, so every backend that edits through it (Postgres too) is bounded and
`replace_all` cannot multiply a file without limit. `write_file` refuses
content past it; a backend's own `write` does not check, so the offload hook
can store a large result. The tools report both as `too_large`, naming the
path, and leave the file as it was.

| Backend | Where files live |
|---|---|
| `MemoryBackend` | Memory, gone with the process |
| `DiskBackend` | Real files under a root, confined like `LocalFs` |
| `linch_postgres::PgFileBackend` | The `linch_files` table, per namespace, shared across processes |
| `CompositeBackend` | Routes by longest path prefix to other backends |
| `ScopedBackend` | One subtree of another backend as its own root |

The tools and the offload hook find a session's backend through
`SessionFiles` (`crates/linch-tools/src/vfs.rs:53`): `MemorySessions` gives
each session a fresh memory backend (call `forget` when it ends), and
`SharedFiles` gives every session one backend.

`vfs_tools(files)` (`crates/linch-tools/src/vfs/tools.rs:35`) returns `ls` and
`read_file` (read scope) and `write_file` and `edit_file` (write scope).
`vfs::PROMPT` (`tools.rs:24`) teaches the model the virtual filesystem; the
host adds it to the system prompt when it offers these tools.

`ResultOffload` (`crates/linch-tools/src/vfs/offload.rs:72`) is a
`post_tool_use` hook. A successful result whose value is a string and is
estimated above `threshold_tokens` (four bytes per token) is written whole to
`/offload/{tool}_{tool_use_id}.txt` and replaced by its first 10 lines and a
pointer the model can follow with `read_file`. Errors, structured values and
the four virtual filesystem tools are never offloaded, so reading an offloaded
file back is never offloaded again (`offload.rs:51-62`).

- The replacement is the call's result from then on: the tool-end event, the
  session log and later requests hold the pointer, not the payload.
- The backend write finishes before the hook answers, so a logged pointer
  names a written file when the backend is durable. A crash between the write
  and the tool end leaves an unreferenced file; a retry of the call writes the
  same path (`offload.rs:9-17`).
- The hook fails open (`offload.rs:119`): a storage failure shows in the hook
  record and the original result goes to the model.
- Its policy id `linch.vfs.offload`, version 1, is part of the run contract.

With `MemorySessions`, a run resumed in another process finds the pointer but
not the file.

## Session task tools

`task_tools(store, namespace)` (`crates/linch-tools/src/tasks.rs:24`) returns
`TaskCreate` and `TaskUpdate` (write scope) and `TaskList` and `TaskGet` (read
scope) over a `TaskStore` (`crates/linch-core/src/store/tasks.rs`). Names,
schemas and the texts the model sees match Python's. Tasks are session data,
not log entries: only the calls and their results enter the session log. The
store contract is on the [stores](./stores.md#task-store) page.

## Write a tool in Rust

This compiles and runs at this revision. It registers a read-scope tool,
drives one run that calls it through `ScriptedProvider`, then disposes the
registration:

```rust
use std::sync::Arc;

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, Event, JsonObject, JsonValue, MemoryRunStore, MemorySessionStore,
    PermissionEngine, PermissionMode, PermissionPolicy, RunOptions, ScriptedProvider, Tool,
    ToolContext, ToolOutput, ToolOutputError, ToolRegistry, ToolResult, ToolScope,
};
use serde_json::json;

/// Counts the words in a text. It only reads, so its calls may run in
/// parallel with other read calls and need no approval.
struct WordCount {
    schema: JsonObject,
}

impl WordCount {
    fn new() -> Self {
        let schema = json!({
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to count."}},
            "required": ["text"]
        });
        let JsonValue::Object(schema) = schema else { unreachable!() };
        Self { schema }
    }
}

#[async_trait]
impl Tool for WordCount {
    fn name(&self) -> &str {
        "word_count"
    }

    fn description(&self) -> &str {
        "Count the words in a text."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        if ctx.cancel.is_cancelled() {
            return Err(ToolOutputError::new("cancelled"));
        }
        let text = input.get("text").and_then(JsonValue::as_str).unwrap_or_default();
        ctx.report_progress("counting", None);
        Ok(ToolOutput::new(text.split_whitespace().count()))
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let tools = Arc::new(ToolRegistry::new());
    let registration = tools.register(Arc::new(WordCount::new())).unwrap();
    assert!(tools.register(Arc::new(WordCount::new())).is_err(), "names are unique");

    let provider = ScriptedProvider::new([
        TurnBuilder::new("m")
            .tool_call("c1", "word_count", &json!({"text": "one two three"}))
            .build(),
        TurnBuilder::new("m").text("Three words.").build(),
    ]);
    let permissions =
        PermissionEngine::new(PermissionPolicy::new(PermissionMode::Default, "/")).unwrap();
    let agent = AgentBuilder::new(AgentSpec::new("m"))
        .provider(Arc::new(provider))
        .tools(Arc::clone(&tools))
        .permissions(permissions)
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("docs")
        .build()
        .unwrap();
    let mut stream = agent.run("How many words?", RunOptions::new("s1"));
    let mut result = None;
    while let Some(item) = stream.next().await {
        if let Event::ToolCallEnd(end) = item.unwrap().event {
            result = Some(end.result);
        }
    }
    assert!(!result.expect("the tool ran").is_error);

    registration.dispose();
    registration.dispose(); // a second dispose does nothing
    assert!(tools.snapshot().resolve("word_count").is_none());
}
```

Wiring the built-in tools follows the same pattern. This also compiles and
runs: it registers the workspace tools, the virtual filesystem tools and the
offload hook, and keeps every handle:

```rust
use std::path::Path;
use std::sync::Arc;

use linch_core::{Hooks, Registration, ToolRegistry};
use linch_tools::vfs::{MemorySessions, OffloadConfig, ResultOffload, SessionFiles, vfs_tools};
use linch_tools::{LocalFs, ReadTracker, workspace_tools};

/// Register the workspace tools over `root`, the virtual filesystem tools,
/// and the offload hook. Keep the handles to undo every registration.
fn wire(root: &Path, tools: &ToolRegistry, hooks: &Hooks) -> Vec<Registration> {
    let fs = Arc::new(LocalFs::new(root).expect("an existing directory"));
    let reads = Arc::new(ReadTracker::new());
    let files: Arc<dyn SessionFiles> = Arc::new(MemorySessions::new());
    let mut registrations = Vec::new();
    for tool in workspace_tools(fs, reads).into_iter().chain(vfs_tools(Arc::clone(&files))) {
        registrations.push(tools.register(tool).expect("names are free"));
    }
    let offload = ResultOffload::new(files, OffloadConfig::new(8_000)).expect("a valid prefix");
    registrations.push(hooks.post_tool_use.register(Arc::new(offload)).expect("a policy id"));
    registrations
}

fn main() {
    let (tools, hooks) = (ToolRegistry::new(), Hooks::default());
    let registrations = wire(&std::env::temp_dir(), &tools, &hooks);
    assert_eq!(tools.snapshot().definitions().len(), 10);
    for registration in &registrations {
        registration.dispose();
    }
    assert!(tools.snapshot().definitions().is_empty());
}
```

The run contract digest covers the tools registered when
`AgentBuilder::build` runs (`crates/linch-core/src/agent/builder.rs:201`). A
tool registered later, such as an MCP tool after a relist, is offered from the
next turn's snapshot but is not part of the contract.

---

Back to the [architecture index](./README.md).

# Changelog

Notable changes to `linch`. Versioning follows the contract in
[docs/versioning.md](docs/versioning.md): the public API is exactly `linch.__all__`,
and persisted formats share a wire generation separate from the package version,
reflected by `linch.RUN_SCHEMA_VERSION`.

## Unreleased

### Fixed

- Bash rules treat more shell syntax as unparsed: such a command can still be denied or asked, and
  is never allowed by a Bash rule. This adds `&>`, `|&`, `&` after an escaped or quoted `<` or `>`,
  `>&` onto a file, an unquoted `#` at the start of a word, here-documents, `>|`, a `${...}` inside
  double quotes whose body holds a quote, a backslash or a nested `${`, a `$` expansion, brace
  expansion, `function` or `coproc` in command position, `a[0]=1`, a `{name}>` redirection and
  `env -S`. A deny rule that follows a non-matching deny rule now also denies such a command.
  Words are separated by space and tab only, and inside double quotes a backslash is kept before
  any character except `$`, backtick, `"`, `\` and newline, so a pattern such as `cat "a\nb"`
  matches only that text.
- A Bash allow rule no longer covers a command segment that redirects output to a file (`>`, `>>`,
  `>|`, `<>`, with or without a descriptor number): the call asks, or falls to the mode default
  when no rule decides it. Descriptor duplication (`2>&1`, `>&2`, `>&-`, `<&3`) and exactly
  `/dev/null`, `/dev/stdout` and `/dev/stderr` are not files. The allow-all rule (the bare
  pattern `*`) is exempt, since it already allows every command; unparsed syntax is never
  allowed, allow-all included. Deny and ask rules are unchanged.
- Bash deny and ask rules see through the words in front of a command: leading assignments and
  redirections, `if then else elif do while until ! {`, and `time exec command builtin env` (quoted
  or not) with their options and option arguments (`command -v`/`-V` is kept whole). The command
  word is matched unquoted (`\rm`, `r""m`) while arguments stay as written, so a deny rule
  `* --force` does not match a commit message that contains `--force`. Allow rules still match
  the segment as written. Regex patterns follow the same rule.
- After `env`, Bash deny and ask rules read any word with an `=` that does not start with `-` as an
  assignment (`a.b=1`, `1X=1`, `a-b=1`), as `env` does; other wrappers keep the identifier check.
- After a wrapper option that takes an argument (`env -u`, `time -o`, `exec -a`), Bash deny and ask
  rules skip redirection words before reading the argument, so `env -u 2>&1 X touch M` is read as
  `touch M`.
- A `${` followed by a blank, tab or `|`, inside or outside double quotes, makes the command
  unparsed for Bash rules, since bash 5.3 reads `${ cmd; }` and `${| cmd; }` as command
  substitutions.
- Bash deny and ask rules match each word unquoted with the word boundaries kept (a word that is
  empty or holds a blank, a quote or a backslash is written back in single quotes), so
  `git push *` covers `git 'push' origin main` and `rm -rf *` covers `rm '-rf' build`, while
  `* --force` still does not match inside `git commit -m 'never --force'`.
- Bash deny and ask rules read wrapper options unquoted (`env "-i" cmd`, `env "X=1" cmd`) and as
  GNU does: bundled short options (`env -iu NAME`, `time -po FILE`, `exec -la NAME`), unique
  prefixes of long options (`env --uns NAME`, `time --outp FILE`) with an optional `=value`, and
  the argument of `env -u`/`-C`/`-a`, `time -f`/`-o` and `exec -a`. `env -S` and
  `--split-string`, quoted or bundled, make the command unparsed.
- `>&` followed by anything other than exactly digits or `-` is a file redirect for Bash allow
  rules (`>&2file`, `>&2-`, `>&-file`), not a descriptor duplication; a narrow allow rule asks and
  the allow-all rule still allows. A blank after `>&` is unparsed.
- A `$[` inside the `${...}` of a double-quoted parameter expansion makes the command unparsed
  for Bash rules, as a quote or a nested `${` already did, since bash nests the arithmetic and a
  `}` inside it does not end the expansion.
- The compaction summary no longer loses early content. The summary request ended with the
  folded history's last user message, which the model answered ("Acknowledged. Received log
  excerpt 2...") instead of summarizing. It now ends with one user message carrying a guard
  against instructions inside the conversation, the summary prompt and, when the folded part
  already begins with a summary, a note to update it (`linch.compaction.summary_instruction`).
  A summary is rejected unless the view after it (header, text and the kept recent turns) is
  estimated strictly smaller than the view before it, as linch-rust's `compaction::rejection`
  decides: four bytes per token over the whole view, not the serialized message (receipt
  `rejected`, reason `not_smaller`, usage still billed; `summarize_with_provider` raises
  `ProviderError` and takes the kept turns as `kept`).
- OpenAI-compatible providers replay tool-call arguments as UTF-8 literal JSON
  (`ensure_ascii=False`) instead of `\uXXXX` escapes, so non-ASCII arguments keep their
  form and token count across turns.
- `AskUser` no longer fails with "questions entries must be objects" whenever any hook is registered:
  `AskUserTool.validate` is now idempotent, so the scheduler's re-validation after `PreToolUse`


## 3.1.3 — 2026-11-07

### Changed (breaking)

- `@tool` and `FunctionTool` require an explicit `scope`. A missing one raises `ConfigError`
  when the tool is built; the old default (`read`) auto-approved side-effecting functions (#99).
- `PostgresFileBackend` takes a required `namespace` and keys files by `(namespace, path)`.
  An old table is rejected, not migrated (#100).
- Offloaded tool results live in the session store, scoped to the root session of the agent tree,
  so they survive a takeover and no longer land in the workspace. Postgres adds the
  `session_offloads` table (#104).

### Fixed

- `DockerBackend` passes `--env KEY` only; values travel in the subprocess environment, not argv (#101).
- A PreToolUse hook returning `retry` or `force_continue` blocks the call instead of running the tool (#102).
- Grep kills and reaps `rg` (its process group) on timeout or cancellation (#103).
- `AgentRegistry` lists a built-in subagent once when an extra built-in shares its name (#105).
- Task tools resolve to the root session of the agent tree, so workers see the coordinator's tasks (#106).
- A continue to a busy worker is refused without changing its status; `TaskStop` acts only on a
  running worker and records the reason; a continuation uses the supplied run id (#107).
- `SubagentContinue` and `TaskStop` no longer reach the worker tool catalog (#108).

## 3.1.2 — 2026-11-03

### Added

- `build-agent-with-linch` includes `references/design.md`, a design step for systems whose
  architecture is still open.

### Documentation

- State what a `linch.worker` job runs: a queued prompt through a session, not a directed
  workflow, and that ending a job cancels unfinished background subagents.
- Cover running `run_workflow` across processes, including crash recovery and lease waits.

## 3.1.1 — 2026-11-02

### Fixed

- vLLM and SGLang apply `max_model_len` to any model alias when the server lists
  exactly one model, so a served alias still gets the real context window.
- The vLLM and SGLang context probe runs through the chat client, so credentials
  are not carried across cross-origin redirects, `OPENAI_API_KEY` and
  `OPENAI_BASE_URL` apply, and the `/models` path matches the chat base URL.
- Concurrent `create_run` calls with one id and session raise the documented
  duplicate-id `ValueError` or adopt the pending row instead of `SessionBusyError`.
- `Read` rejects NUL bytes anywhere in a file, not only in its first 8 KB.
- `Edit` refuses to write when the file changed after its freshness check.
- `BashProcess` `kill` no longer reports a process that already failed as a success.
- `run_in_background` at `max_processes` is refused before spawning a process.
- Terminal cleanup failures no longer form a cycle in the exception cause chain.
- MCP text above the 64 MiB spill ceiling skips spill work and goes to the plain cap.
- `Agent(tools=...)` raises `ConfigError` for any non-`None` value other than a
  `ToolRegistry`.
- Local and DeepSeek adapters declare their own capabilities, context windows,
  reasoning, and usage semantics instead of inheriting OpenAI model assumptions.
- vLLM streams prefer `reasoning` with a `reasoning_content` fallback and preserve
  `reasoning_content` in assistant history. Cache-creation tokens populate the
  existing usage bucket without double-counting prompt tokens; llama.cpp retains
  `timings.cache_n` cache-read accounting.
- vLLM and SGLang warn once per provider instance when prompt-cache details are
  missing, naming the required server launch flag. Zero with that warning means
  unknown reuse, not a confirmed miss.
- SGLang no longer sends `enable_cache_report` as a request field; the options
  field remains accepted but reporting requires the server's `--enable-cache-report`.
  `sampling_params` fields are flattened into `extra_body`, with explicit
  `extra_body` values taking precedence.
- vLLM and SGLang use configured model-specific template kwargs for disabled
  thinking and advertise that control only when `disable_thinking_kwargs` is set.

### Changed

- DeepSeek, llama.cpp, vLLM, and SGLang are no longer subclasses of
  `OpenAIChatCompletionsProvider`. A private base shares transport, serialization,
  and stream mechanics only; OpenAI keeps its own known-model defaults.
- vLLM and SGLang now probe `/v1/models` by default during asynchronous preparation
  for per-model context capacity. Explicit `context_window` skips discovery;
  `auto_context_window=False` opts out. Failed or unmatched probes fall back to
  128,000 tokens, and synchronous context lookups never access the network.
- New option fields follow the original positional fields, preserving existing
  positional construction. Public exports, usage schema, and persisted formats
  are unchanged. `stream_partials=False` still consumes the same upstream stream;
  this release adds no non-streaming transport.
- English and Vietnamese provider guides document launch flags, cache reporting,
  context discovery, template controls, and existing cache-tuning observations.

## 3.1.0 — 2026-11-02

This MINOR release adds execution capabilities with the behavior changes below.
Review host policy and drain all active runs before upgrading.

- Managed process IDs are ephemeral UUID-derived handles; finished handles are bounded to 128. Cleanup precedes durable terminal status and failures preserve admission for retry. Execution limits participate in resume identity.
- Shell and MCP spills use asynchronous, symlink-safe mode-0600 files. MCP reconnect has bounded attempts and connection timeouts; changed tool lists require a new session.
- Postgres admission bounds retries when a conflicting run disappears before it can be read.


- **Managed shell processes.** With a live-process backend and available process
  capacity, Bash `timeout_ms` returns output and a process ID while the command
  keeps running; follow it with `BashProcess` wait/status/kill. Explicit
  `run_in_background` returns immediately. At capacity, a still-running command
  is killed when the call returns, with partial output as an error. Without live
  handles, Bash delegates timeout handling to the backend's `run` method;
  Local/Docker kill on timeout. The default is four running processes and a
  30-minute hard runtime limit. Handles are run-owned and process-local: run
  completion cleans up process groups and shell spill files. Cleanup failures
  keep the session active for a close retry; Docker removal is best effort. A
  restart loses handles, and a hard worker kill can leave children behind. Backends implementing only `run` remain
  supported as-is; `ShellStarter`/`ShellProcess` is an optional capability.
- **Local shell environment.** `LocalBackend` now forwards only PATH, HOME, TERM,
  LANG, LC_ALL, LC_CTYPE, USER, SHELL, and TMPDIR by default. Pass other values
  explicitly through `env` or `forward_env`. When either is non-empty, durable
  runs need a stable secret `resume_fingerprint_key` to key environment-value fingerprints.
  This environment hygiene does not sandbox local commands. Shell stdin is
  closed, and timeout/abort cleanup preserves bounded partial output.
- **MCP trust and permissions.** Server annotations are ignored by default;
  tools have `exec` scope, run serially, and ask in `default` and `acceptEdits`
  modes. Hosts can explicitly allow a server with
  `ToolRule("mcp__server__*", "allow")`, or vouch for its annotations with
  `trust_annotations=True` to allow declared read-only tools to run as parallel
  reads. Hints remain server claims; trusted false claims can grant unsafe
  read authority. Calls observe abort and a per-call timeout configured on the
  server (300 seconds by default). Lost connections trigger bounded reconnect
  attempts; the failed call is never replayed, and removed tools report errors.
- **Run cancellation and admission.** A second nonterminal run on a session
  raises `SessionBusyError` with the existing run ID; `suspended` runs also
  block admission. Hosts must queue prompts or report busy. Cancelling an
  in-process task leaves its durable session busy until the host resumes that
  run or reconciles side effects and calls `mark_failed`. On client disconnect,
  use `session.abort()` and continue consuming through the final result.
  Postgres installs a partial unique admission index on compatible version-3
  schemas; reconcile duplicate nonterminal runs with the old deployment before
  upgrading, or initialization fails.
  Workers default to 32 concurrent queue tasks per process (minimum two).
- **Docker defaults.** Commands now receive `memory="2g"`, `cpus=2`,
  `pids_limit=512`, `cap_drop=("ALL",)`, and `no_new_privileges=True`. Review
  workloads and set individual controls to `None` when disabling them is intended.
- **File freshness.** With a file-read tracker, overwriting an existing file
  with workspace `Write` requires a prior whole-file read and a current
  modification-time/size stamp. `Edit` also rejects externally changed files;
  re-read before retrying. `Read` rejects binary files and files above its
  default 10 MiB limit; use Grep to narrow oversized inputs.
- **Bounded output and skills.** Shell output retains a bounded head/tail;
  oversized shell and MCP text can spill under `.linch/output/` for inspection.
  Spilling is best effort and discards files above 64 MiB. MCP embedded text
  resources retain their text. Skill bodies substitute `${SKILL_DIR}` for
  bundled scripts run through Bash and its normal permissions; quote paths and
  ensure the directory is visible to the execution backend.
- **Upgrade and resume.** Drain to zero active runs before installing 3.1.0:
  Local/Docker backend policy versions are now `2`, and Bash tool schemas have
  changed. Package-version-only and top-level tool-description-only changes
  no longer invalidate otherwise matching resume contracts. Wire version 3,
  tool names/input and output schemas/scope/concurrency, permission policy,
  and execution backend identity still gate resume. Compatible version-3
  databases can be reused; this release does not require a fresh database.

## Earlier releases (condensed)

Full notes for earlier releases are in the git history and the GitHub releases. The
breaking-change and migration notes of the current major (3.0.0 and 3.0.0a1) are kept
in full below.

### 3.0.2 — 2026-11-02

Added the `build-agent-with-linch` skill with `linch skill install`; shipped the usage and architecture docs and examples with the wheel (`linch docs path` locates them); MCP transports now close safely from any task.

### 3.0.1 — 2026-11-02

Added async `Agent.reload_skills()`; each session's system blocks and tool schemas are frozen in durable metadata so the provider prefix survives restarts; added off-by-default `native_catalog_changes` for Anthropic and OpenAI Responses (unverified against live endpoints).

### 3.0.0 — 2026-11-01

Kernel and plugins, versioned profiles, deferred tool search, per-run `Principal`, lease-fenced durable runs, the `linch.worker` fleet runtime, and OpenTelemetry instrumentation; Python 3.11+ and wire generation 3 are required, and the release fixes the findings of the 3.0 release reviews, some present in 2.0, with no 2.x backport. The breaking and new features list is kept in full; the fixed and development entries are in the git history.

#### Breaking and new features

- **Kernel and plugins.** `Context`, reversible `Disposable`, typed lifecycle bus, and `Plugin` mounts through `agent.use()` are now the extension foundation; see [the kernel guide](docs/architecture/kernel.md#plugins).
- **Versioned profiles.** `Profile` and `RunOptions(profile=...)` select immutable per-run roles, plugin rows, tool restrictions, budgets, schemas, and resume-pinned content; see [domain profiles](docs/usage/profiles.md).
- **Deferred tool search.** `ToolSearchPlugin` keeps small catalogs byte-identical and defers large tool/skill schemas until `tool_search` loads them; see [tool search](docs/usage/tools.md#tool-search-for-large-catalogs).
- **Lifecycle catalog and tracing.** Typed lifecycle points have a generated producer/consumer catalog, and `trace_id` follows events, logs, tools, jobs, and subagents; see [events](docs/architecture/events.md#typed-lifecycle-catalog).
- **Per-run identity and RBAC.** `Principal(tenant, user, roles)` is carried through `RunOptions`, permission rules, tool context, and the durable run contract; see [role rules](docs/usage/tools.md#per-run-principals-and-role-rules).
- **RAG and grounding.** `RagConnector` returns sourced chunks and optional final-answer grounding enforces `[doc:<id>]` citations; see [context and memory](docs/usage/context-and-memory.md#sourced-retrieval-and-grounding).
- **Durable run ownership.** Postgres runs use lease epochs, fenced writes, checkpoints before side effects, and reconnect-by-sequence event streams; see [run stores](docs/usage/run-stores.md).
- **Worker fleet runtime.** Optional `linch.worker` uses PgQueuer for dispatch, leases, retries, graceful drain, cron, and takeover after worker/database failures; see [production scale-out](docs/usage/production.md#scale-out).
- **File-backed memory.** Scoped `MEMORY.md` indexes are loaded through the filesystem backend; semantic recall is an optional host plugin rather than a core memory store; see [file memory](docs/usage/context-and-memory.md#file-memory).
- **OpenTelemetry instrumentation.** Core uses `opentelemetry-api` at run/turn/provider/tool/compaction boundaries; hosts own SDK/exporter setup and message-content capture policy; see [traces and metrics](docs/usage/events.md#opentelemetry-traces-and-metrics).
- **Explicit extension namespace.** Workflows, evals, deep-agent presets, reports, loop runners, and the scaffold CLI live under `linch.ext.*`; see [the extension boundary](docs/architecture/overview.md#extensions).
- **Neutral agent defaults and tool protocols.** Bare agents have no domain tools or discovery features by default; `workspace_tools()`, duck-typed tools, reversible registry handles, hooks, and explicit plugin mounts make trust boundaries opt-in; see [tools](docs/usage/tools.md).
- **Reliability and finalization.** Default response reliability, prerequisites, completion gates, and the serial `turn-stopping` seam make malformed or incomplete responses observable and recoverable; see [reliability](docs/usage/agent.md#tool-and-response-reliability).
- **Unified execution world.** Shell and filesystem access use coherent `ExecutionBackend` transports with durable policy identity; see [execution backends](docs/usage/extending.md#executionbackend--one-shell--filesystem-world).
- **Append-only session state.** `SessionLog` is the durable source for detached `provider_view`/`full_history` projections; per-request RAG/context remains ephemeral; see [session invariants](docs/architecture/invariants.md).
- **Canonical tool output and durable delivery.** JSON Schema tool results, progress/attachments, durable inbox notifications, and exact pending model-input snapshots are available through the Postgres capabilities; see [events](docs/usage/events.md).
- **Accounting and compaction.** Model-call receipts settle cumulative primary/compaction usage before budget admission, with fail-closed unknown usage and durable built-in compaction replay; see [accounting and compaction migration](docs/migration-3.0.md#accounting-and-compaction).
- **Wire and runtime floor.** Python 3.11+ and wire generation 3 are required; pre-3.0 persisted data and legacy readers are rejected; see [Migrating to 3.0](docs/migration-3.0.md).
- **Provider and dependency changes.** Gemini/Vertex AI use `google-genai`, durable state is Postgres plus InMemory for tests/dev, and SQLite stores are removed; see [provider setup](docs/usage/providers.md) and [run stores](docs/usage/run-stores.md).

See [Migrating to 3.0](docs/migration-3.0.md) for replacement imports,
removed compatibility paths, and required durable-state changes.

### 3.0.0a1 — 2026-10-30

First 3.0 prerelease: cumulative model-call usage across primary and compaction calls, public `ModelCallEvent` journal records, budget-safe compaction with durable receipts, and fail-closed unknown usage. Its migration notes are kept in full.

#### Migration notes

- No prompt, persona, default-strategy, or normal primary-input behavior
  changes in this prerelease. Existing custom strategies remain supported when
  they use the supplied provider facade for calls that should be accounted.
- Unknown or unpriced models do not receive a synthetic USD price. Token usage
  remains known when reported, while aggregate cost is partial and cost caps
  cannot enforce an unknown price.
- Receipt ownership is physical delivery ID, not logical operation ID. Retries
  and fallbacks that reach final usage are separate billed deliveries.
- Durable runs with an old in-flight accounting shape fail closed, including
  when legacy resume is explicitly opted into; start a fresh run or migrate the
  durable state under the matching contract.
- This release does not promise distributed-tree cap recovery, exactly-once
  external-provider billing, or reservations against concurrent admissions.

### 2.0.0 — 2026-09-14

Breaking: neutral `Agent` defaults (empty tool registry, in-memory session store, features off), unified `ExecutionBackend` with `.shell` and `.fs`, strict persisted-data and provider-stream reads; added `Context`, `ToolPipeline`, `SessionLog`, fail-closed `RunContract` resume, and canonical, schema-validated JSON tool output (`jsonschema` is a core dependency). See [migration-2.0.md](docs/migration-2.0.md).

### 1.2.1 — 2026-08-12

`linch[mcp]` requires `mcp>=2.0.0` (pin `linch<=1.2.0` to stay on mcp 1.x); added `Agent(limiter=...)` and `Agent(max_provider_concurrency=N)`; fixed a leaked MCP connect on cancellation and discarded MCP tool input schemas.

### 1.2.0 — 2026-08-11

Workflow `wf.step`, `wf.interrupt` (durable human-in-the-loop), `wf.settled`, per-call `timeout_ms` and `retry`, and `run_workflow` deadline, signal, concurrency and journal-snapshot options; added `CheckpointableHook`; fixed `wf.parallel` leaking work and nested fan-out deadlocks.

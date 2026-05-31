# AgentKit Architecture

Deep-dive reference for contributors. Covers the full data flow, every subsystem's contract, and the key design invariants that must not break.

---

## Bird's-eye view

```
Caller
  │
  ├─ Agent(config)           ← immutable after init
  │     model, provider, tools, permissions,
  │     system_prompt_config, context_injectors,
  │     deps, output_schema, features
  │
  └─ session = await agent.session()   ← mutable per-conversation state
        │
        └─ async for event in session.run(prompt, RunOptions?):
              │
              └─ run_loop()            ← the engine
                    │
                    ├─ _re_inject_skill_context()
                    ├─ _run_context_injectors()   ← mutates provider_view
                    ├─ _build_turn_request()
                    ├─ provider.stream(req)
                    ├─ assemble response
                    ├─ permission check  (PermissionEngine)
                    ├─ scheduler.execute_tools()
                    └─ yield events → caller
```

Every cross-cutting concern (compaction, skills, subagents, structured output, deps) plugs into one of these seams rather than scattering through the loop.

---

## Module inventory

| Module | Responsibility |
|--------|---------------|
| `agent.py` | Immutable config object; builds system blocks; owns `session()` factory |
| `session.py` | Per-conversation state: `provider_view`, `full_history`, `run_deps`, `RunOptions` |
| `loop.py` | Main agent loop: turn orchestration, event emission, compaction trigger |
| `types.py` | All shared dataclasses: `Message`, `ContentBlock`, `ProviderRequest`, `OutputSchema` |
| `events.py` | All event dataclasses + `event_to_dict`/`event_from_dict` |
| `config.py` | `FeatureFlags`, `SystemPromptConfig` |
| `context_hooks.py` | `ContextInjector` protocol, `TurnContext`, `prune_tagged` |
| `scheduler.py` | Parallel tool execution with concurrency cap |
| `compaction.py` | Context-window management; calls `agent.provider` directly |
| `permissions/engine.py` | Rule evaluation; emits `PermissionRequestEvent` and pauses loop |
| `providers/` | `BaseProvider` + OpenAI Chat, OpenAI Responses, Anthropic, Retry |
| `tools/` | Tool protocol, `ToolContext`, `ToolRegistry`, built-in tools |
| `sessions/` | `SessionStore` protocol + `InMemorySessionStore` + `SqliteSessionStore` |
| `mcp/` | MCP server connection → AgentKit tool adapters |
| `skills/` | `SKILL.md`-based slash-commands with argument substitution |
| `subagents/` | Specialized agent roles from `.agent_kit/agents.yaml` |
| `recipes/` | Factory helpers; not part of the loop — purely additive |

---

## Data flow: one turn in detail

```
run_loop() top of turn N
│
├─ 1. _re_inject_skill_context(session)
│      – re-inserts the active skill's system reminder into provider_view
│      – no-op when no skill is active
│
├─ 2. _run_context_injectors(session, turn_index, extra_system)
│      – fires each ContextInjector.before_turn(TurnContext)
│      – injectors may append/prune Messages in provider_view
│      – injectors may append SystemBlocks into extra_system list
│
├─ 3. _build_turn_request(session, opts, extra_system, model_override)
│      – merges agent.system_blocks + extra_system → ProviderRequest.system
│      – copies session.provider_view → ProviderRequest.messages
│      – threads output_schema, tool_choice, thinking, effort from opts/agent
│
├─ 4. provider.stream(req) → AsyncIterator[StreamEvent]
│      – normalized dicts: {"type": "text_delta"|"tool_use"|"usage"|"stop", ...}
│
├─ 5. assemble AssistantAssembly(message, stop_reason, usage)
│      – collects TextBlocks and ToolUseBlocks
│      – emits partial_assistant events during streaming
│
├─ 6. final_tool_name interception (if set)
│      – if any ToolUseBlock.name == final_tool_name:
│          structured_output = block.input
│          emit ResultEvent(structured_output=...) and return
│      – tool is NOT dispatched to scheduler
│
├─ 7. PermissionEngine.check_all(tool_calls) → approved / pending
│      – emits PermissionRequestEvent and suspends until caller responds
│
├─ 8. scheduler.execute_tools(approved_calls, session)
│      – each tool.execute(input, ToolContext(deps=session.run_deps))
│      – parallel if all are parallel_safe; otherwise serial
│      – respects AGENTKIT_MAX_TOOL_CONCURRENCY
│
├─ 9. update session.provider_view + full_history
│
└─ 10. emit events; continue loop if stop_reason == "tool_use"
        stop when stop_reason == "end_turn" (text-only response)
```

### Two views of history

`session.provider_view` — trimmed list sent to the LLM each turn. Compaction modifies only this list (old messages replaced by a summary). Context injectors also mutate this list per-turn.

`session.full_history` — append-only complete record. Never sent to the provider; used for auditing and session persistence.

**Invariant:** `full_history` is a strict superset of the logical conversation. Do not modify it outside `loop.py`.

---

## Provider contract

Every provider implements `BaseProvider` (two methods):

```python
class BaseProvider(Protocol):
    def context_window(self, model: str) -> int: ...
    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict]: ...
```

`stream()` yields normalized dicts — not raw API objects. Required keys by type:

| `type` value | Required keys |
|---|---|
| `"text_delta"` | `text: str` |
| `"tool_use"` | `id: str`, `name: str`, `input: dict` |
| `"usage"` | `input_tokens: int`, `output_tokens: int` |
| `"stop"` | `stop_reason: StopReason` |
| `"thinking_delta"` | `thinking: str` |

The loop in `loop.py` assembles these — it never touches raw API types. Adding a new provider means implementing this dict contract only.

### RetryProvider

Wraps any `BaseProvider` with exponential-backoff retry (connect + first chunk only — retrying mid-stream is unsafe). Honors `ProviderRequest.max_retries` and `RateLimitError.retry_after_seconds`.

---

## Tool protocol

Tools are **duck-typed** — no base class required. Required interface:

```python
class MyTool:
    name: str                    # unique identifier
    description: str             # shown to the model
    input_schema: dict           # JSON Schema
    scope: Literal["read","write","exec"]
    parallel_safe: bool          # can run alongside other parallel tools

    def validate(self, raw: dict) -> dict: ...          # raise ValueError on bad input
    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult: ...
    def summarize(self, input: dict) -> str: ...        # one-line description for logs
```

`ToolContext` carries: `cwd`, `session_id`, `run_id`, `session_store`, `signal` (abort), `file_read_tracker`, `deps`.

`deps` is set from `session.run_deps` by the scheduler — it is whatever the caller passed as `Agent(deps=...)` or `RunOptions(deps=...)`.

### ToolRegistry

```python
registry.register(tool)           # add
registry.unregister(name)         # remove by name
registry.replace(tool)            # swap (same name)
registry.copy()                   # shallow clone
registry.subset(include, exclude) # filter by name set

# Module-level factories
empty_tools(*extra)               # no built-ins + optional extras
tools_from_defaults(exclude, extra)  # standard set ± named tools
```

---

## System prompt construction

`Agent._build_system_blocks(tool_names)` assembles the system from four layers, in order:

1. **Custom blocks** — `SystemPromptConfig.blocks` (prepended before identity)
2. **Identity block** — "You are AgentKit…" (omitted if `replace_defaults=True`)
3. **Protocol block** — tool-use instructions (omitted if `replace_defaults=True` or if no SWE tools present); clauses are conditional on which tool families are registered
4. **Append block** — `SystemPromptConfig.append` or legacy `Agent(system_prompt=...)`

When `replace_defaults=True`, only blocks 1 and 4 are emitted. Use this for any non-SWE agent.

The protocol block is **tool-aware**: it only describes tools that are actually registered. An agent with only `Read` + custom tools will not see Edit/Bash instructions in its system prompt.

**Invariant:** when the full default toolset is registered and `replace_defaults=False`, the protocol block text must be byte-identical to the hardcoded reference in `tests/test_system_blocks.py`. If you change the wording, update the parity test.

---

## Context injection

`ContextInjector.before_turn(ctx: TurnContext)` fires before every provider call. It receives `TurnContext`:

```python
@dataclass(slots=True)
class TurnContext:
    session: Session
    provider_view: list[Message]   # same object as session.provider_view — mutate freely
    turn_index: int
    deps: Any                      # session.run_deps
    extra_system: list[SystemBlock] # append here; merged into system for this turn only
```

**Key rules:**
- `provider_view` mutations persist across turns (intended — injectors own their region).
- `extra_system` is ephemeral — it is built fresh each turn and not stored.
- Use `prune_tagged(provider_view, tag)` to remove a prior injection before appending a new one. Without pruning, context grows unboundedly.
- Multiple injectors fire in registration order; each sees the mutations of the prior.
- Injectors must not block — use `await` for async I/O.

---

## Permissions

`PermissionEngine.check_all(pending_calls)` runs before the scheduler. Each call is evaluated against the rule list in order:

1. `ToolRule(tool, decision)` — match by tool name
2. `PathRule(paths, decision)` — match by file path glob
3. `BashRule(patterns, decision)` — match bash command patterns
4. Mode default: `"skip-dangerous"` → auto-approve; `"acceptEdits"` → approve file ops, ask for Bash; `"default"` → always ask

When a call is not auto-approved, a `PermissionRequestEvent` is emitted and `run_loop` suspends until the caller calls `session.respond_to_permission(call_id, approved)`.

---

## Structured output

Two paths, both surface as `ResultEvent.structured_output: dict | None`:

**Text-parse path** (`output_schema` on Agent/RunOptions): the provider is instructed to emit JSON (via `response_format` for Chat API, `text.format` for Responses API). `run_loop` parses `final_text` as JSON after the turn. Validation uses `jsonschema` if installed; otherwise parse-only with `structured_error` set.

**Forced-tool path** (`final_tool_name` on Agent/RunOptions): the model is forced to call a specific tool (set `tool_choice` to match). When `loop.py` sees a `ToolUseBlock` whose name equals `final_tool_name`, it reads `block.input` directly as `structured_output` and returns — the tool is **not** dispatched to the scheduler. This path works across all providers and is more reliable for complex schemas.

---

## Compaction

`maybe_compact(session, agent)` is called at the top of each turn. It:
1. Counts tokens in `provider_view` via `agent.provider.context_window(agent.model)`.
2. If within threshold, no-op.
3. Otherwise, calls `_run_compaction_impl(session, agent)` which submits a summarization request via `agent.provider.stream()` and replaces old messages in `provider_view` with the summary.

**Key invariant:** `full_history` is never modified. Only `provider_view` shrinks.
**Key invariant:** `agent.provider` is used for summarization — not a hardcoded OpenAI call.

---

## Sessions and storage

`SessionStore` protocol:

```python
class SessionStore(Protocol):
    async def get(self, id: str) -> SessionData | None: ...
    async def save(self, data: SessionData) -> None: ...
    async def list(self) -> list[SessionData]: ...
```

`InMemorySessionStore` — dict-backed, ephemeral.  
`SqliteSessionStore` — uses `asyncio.to_thread` for all DB operations; safe for async loops.

Tasks (for `TaskCreate`/`TaskList`/etc.) are also stored via `SessionStore` in the same backing store.

---

## MCP integration

`connect_mcp_servers(configs)` returns an `McpConnection`. Each MCP tool is wrapped as an AgentKit tool (duck-typed, not subclassed). Tool names are normalized via `mcp/naming.py` to avoid collisions. The connection is closed on `agent.close()`.

MCP is gated by `FeatureFlags(mcp=True)` in `agent.session()`.

---

## Skills and subagents

**Skills** are loaded from `.agent_kit/skills/*/SKILL.md`. Each file has YAML frontmatter (name, description, allowed_tools, model_override) and a markdown body. When a skill is invoked, the body is injected as a system reminder per-turn via `_re_inject_skill_context`.

**Subagents** are defined in `.agent_kit/agents.yaml`. `subagents/runner.py` creates a child agent with its own tool overlay and system prompt. The child's system blocks are computed via `agent.build_system_blocks_for_tool_names(child_tool_names)` — not copied from the parent — so the child gets a tool-scoped protocol.

Both are gated by `FeatureFlags`.

---

## Key invariants (must not break)

1. **`full_history` is append-only** — only `loop.py` appends; never write to it elsewhere.
2. **`provider_view` is the only thing compaction mutates** — `full_history` is untouched.
3. **Tool protocol is duck-typed** — do not add a base class or `isinstance` check; check for attribute presence.
4. **`stream()` yields normalized dicts, not API objects** — the loop must not import any provider's raw types.
5. **Default SWE system-block text is pinned** — `test_system_blocks.py` has a byte-identical parity assertion; change it only intentionally.
6. **`final_tool_name` tool is never scheduled** — the loop intercepts it before the scheduler.
7. **Context injectors do not use `full_history`** — they work on `provider_view` only.
8. **`run_deps` is set once per `run_loop` call** — at the top, from `opts.deps ?? agent.deps`.

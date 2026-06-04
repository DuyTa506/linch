# Linch Architecture

Professional reference for the V2 harness. Covers every subsystem, its contract, the complete data flow, and the invariants that must not break.

---

## 1. System Overview

The framework is a harness of pluggable subsystems composed around a single event-driven loop. The caller only interacts with `Agent` (config) and `Session` (state); everything else is internal machinery wired together inside `run_loop`.

```mermaid
graph TD
    subgraph Host["Host Application"]
        Caller["Caller"]
    end

    subgraph Core["Linch Core"]
        Agent["Agent\nmodel · provider · tools\npermissions · loop_guard\ncontext_builder · deps"]
        Session["Session\nprovider_view · full_history\nrun_deps · active_run_id"]
        RunLoop["run_loop()"]

        subgraph Pipeline["Turn Pipeline"]
            SK["_re_inject_skill_context()"]
            CB["ContextBuilder\nRAG · budget · tool select"]
            BTR["_build_turn_request()\n+ capability downgrade"]
            PERM["PermissionEngine\nrule eval · loop pause"]
            SCHED["Scheduler\nparallel · serialize · resource lock"]
            LG["LoopGuard\nloop detection"]
        end

        subgraph Providers["Providers"]
            OAR["OpenAIResponsesProvider"]
            OAC["OpenAIChatCompletionsProvider"]
            ANT["AnthropicProvider"]
        end

        subgraph Storage["Storage"]
            SS["SessionStore\nInMemory · SQLite"]
        end

        subgraph Knowledge["Knowledge"]
            MEM["MemoryStore\nkeyword · sqlite · custom"]
        end

        subgraph Filesystem["Filesystem"]
            FS["FileBackend\nState · Disk · SQLite · Composite"]
            OFF["OffloadConfig\nthreshold · preview · prefix"]
        end

        subgraph Extensions["Extensions"]
            MCP["MCP Servers"]
            SKILLS["Skills\n.linch/skills/"]
            SUBA["Subagents\n.linch/agents.yaml"]
        end
    end

    Caller -->|"Agent(config)"| Agent
    Agent -->|"session()"| Session
    Session -->|"run(prompt)"| RunLoop
    RunLoop --> SK
    SK --> CB
    CB --> BTR
    BTR --> OAR & OAC & ANT
    RunLoop --> PERM
    PERM --> SCHED
    SCHED --> LG
    RunLoop -.->|"AsyncIterator[Event]"| Caller
    Session <-->|"persist / load"| SS
    CB <-->|"recall / upsert"| MEM
    Agent <--> MCP & SKILLS & SUBA
    SCHED <-->|"offload / read"| FS
    OFF -.->|"config"| SCHED
```

---

## 2. Turn Lifecycle

One complete agent turn — from receiving the prompt to deciding whether to continue looping.

```mermaid
sequenceDiagram
    actor Caller
    participant RL as run_loop()
    participant CB as ContextBuilder
    participant PR as Provider
    participant PE as PermissionEngine
    participant SC as Scheduler
    participant LG as LoopGuard

    Caller->>RL: session.run(prompt, RunOptions?)
    RL-->>Caller: SystemEvent
    RL-->>Caller: UserEvent

    loop Each turn
        RL->>CB: build(ContextBuildTurn)
        CB-->>RL: ContextBuildResult
        RL-->>Caller: ContextBuildEvent

        Note over RL: _build_turn_request()<br/>+ apply_provider_capabilities()

        RL->>PR: stream(ProviderRequest)
        PR-->>Caller: PartialAssistantEvent (streaming)
        PR-->>RL: AssistantAssembly(message, stop_reason, usage)

        RL-->>Caller: AssistantEvent + UsageEvent

        alt stop_reason = end_turn
            RL-->>Caller: ResultEvent(success)
        else stop_reason = tool_use
            RL->>PE: check_all(tool_calls)
            PE-->>Caller: PermissionRequestEvent (if pending)
            PE-->>RL: approved calls

            RL->>SC: execute_tool_calls(approved_calls)
            SC-->>Caller: ToolCallStartEvent x N
            Note over SC: maybe_offload() — on by default<br/>threshold = context_window × 0.1 (resolved at Agent init)<br/>if tokens(result) > threshold: write to FileBackend, replace with preview + path
            SC-->>Caller: ToolCallEndEvent x N (result=preview, tool_result=full)
            SC-->>RL: result_blocks (previews only enter provider_view)

            RL->>LG: evaluate_loop_guard(state, tool_blocks, result_blocks)

            alt action = stop
                LG-->>RL: LoopGuardDecision(stop)
                RL-->>Caller: LoopGuardEvent
                RL-->>Caller: ResultEvent(error)
            else action = force_final
                LG-->>RL: LoopGuardDecision(force_final)
                RL-->>Caller: LoopGuardEvent
                Note over RL: inject reminder message<br/>strip tools next turn
            else action = continue
                LG-->>RL: LoopGuardDecision(continue)
                Note over RL: append results to provider_view<br/>loop back to top
            end
        end
    end
```

---

## 3. Subsystems

### 3.1 Scheduler — Parallel & Serialized Execution

The scheduler is the only place tool calls are executed. It enforces concurrency policies and resource conflict rules before dispatching.

```mermaid
flowchart TD
    IN["Incoming ToolUseBlocks\n(from AssistantAssembly)"]

    RESOLVE["Resolve each call\n• validate JSON input\n• look up tool in registry\n• collect ResourceAccess declarations"]

    CLASSIFY{{"Classify\ntool scope"}}

    READ["scope = read\nAND parallel = True"]
    WRITE["scope = write or exec\nOR parallel = False"]

    RES{{"ResourceAccess\nconflict check"}}

    PAR["Run concurrently\nup to max_tool_concurrency\n(default: CPU count)"]
    SER["Serialize\none at a time\nwait for prior to finish"]

    OUT["Collect ToolCallEndEvents\nin original provider call order"]

    IN --> RESOLVE --> CLASSIFY
    CLASSIFY -->|"read + parallel"| READ
    CLASSIFY -->|"write / exec"| WRITE
    READ --> RES
    WRITE --> RES
    RES -->|"no resource conflict"| PAR
    RES -->|"same resource\nconflicting mode"| SER
    PAR --> OUT
    SER --> OUT
```

**Rules:**
- `scope="read"` + `parallel=True` → may run concurrently up to `Agent(max_tool_concurrency=N)` or env `AGENTKIT_MAX_TOOL_CONCURRENCY`.
- `scope="write"` or `scope="exec"` → always serialize, regardless of `parallel` flag.
- `ResourceAccess(resource, mode)` enables finer conflict detection: two `"read"` accesses on the same resource overlap freely; any `"write"` on a resource being read or written by another call serializes.
- Result events are emitted in the **original provider tool-call order**, not completion order.
- **Timeouts** — `Agent(tool_timeout_ms=N)` (env `AGENTKIT_TOOL_TIMEOUT_MS`) sets an agent-wide execution deadline. Per-tool override: `execution_timeout_ms` class attribute (`0` = opt-out). Timeout → `is_error=True` result, run continues. Uses `asyncio.wait_for` (Python 3.10 safe). `ToolTimeoutError` (`retryable=True`) is the typed exception class.
- **Retry** — `Agent(tool_retry=RetryOptions(...))` enables opt-in exponential-backoff retry. Read-scope tools retry any exception; write/exec tools only retry when the tool sets `retryable = True`. `AbortError` is never retried.

---

### 3.2 Loop Guard — Agentic Loop Detection

Detects obvious runaway loops cheaply (no extra LLM call) and terminates cleanly.

```mermaid
stateDiagram-v2
    direction LR

    [*] --> Running : run_loop() starts\nguard = LoopGuard() by default

    Running --> Evaluating : tool batch completed\nevaluate_loop_guard()

    Evaluating --> Running : action = continue\nno threshold crossed

    Evaluating --> ForceFinalPending : action = force_final\nemit LoopGuardEvent\ninject reminder message

    ForceFinalPending --> Running : next turn\nreq.tools cleared\nreq.tool_choice cleared

    Running --> Stopped : stop_reason = end_turn\nor final_tool intercepted

    Evaluating --> Stopped : action = stop\nemit LoopGuardEvent\nemit ResultEvent(error)

    Stopped --> [*]
```

**Trip conditions** — `evaluate_loop_guard` checks after every tool batch:

| Check | Threshold | Config field |
|---|---|---|
| Repeated identical call | `call_counts[name:sorted_json] >= N` | `max_identical_tool_calls` (default `3`) |
| Consecutive failure streak | all tools errored for N batches in a row | `max_consecutive_failures` (default `3`) |
| Max turns | `range(max_turns)` exhausted | `Agent(max_turns=N)` emits `LoopGuardEvent(reason="max_turns")` |

`force_final_answer=True` injects a `<system-reminder>` message and strips `req.tools = []` for one final turn so the model must answer in text.

---

### 3.3 Provider Capabilities — Request Downgrade

Every provider declares its feature support. `_build_turn_request` applies downgrades so no provider receives flags it cannot handle.

```mermaid
flowchart LR
    OPTS["Agent / RunOptions\noutput_schema\ntool_choice\ncache_prompt = True\ncache_ttl"]

    REQ["ProviderRequest\n(initial build)"]

    CAPS["provider.capabilities(model)\nProviderCapabilities"]

    APPLY["apply_provider_capabilities(req, caps)"]

    C1{{"prompt_cache\n= False?"}}
    C2{{"tool_choice\n= False?"}}
    C3{{"structured_output\n= False?"}}

    D1["clear cache_prompt\nclear cache_ttl"]
    D2["clear tool_choice"]
    D3["clear output_schema\nloop text-parses response instead"]

    FINAL["ProviderRequest\n(provider-ready)"]

    OPTS --> REQ --> CAPS --> APPLY
    APPLY --> C1
    C1 -->|"yes"| D1 --> C2
    C1 -->|"no"| C2
    C2 -->|"yes"| D2 --> C3
    C2 -->|"no"| C3
    C3 -->|"yes"| D3 --> FINAL
    C3 -->|"no"| FINAL
```

**Declared capabilities per provider:**

| Provider | `prompt_cache` | `structured_output` | `tool_choice` |
|---|---|---|---|
| `OpenAIResponsesProvider` | ✗ | ✓ | ✓ |
| `OpenAIChatCompletionsProvider` | ✗ | ✓ | ✓ |
| `LlamaCppProvider` | ✗ | ✓ | ✓ |
| `AnthropicProvider` | ✓ | ✗ | ✓ |

> **`structured_output` means native JSON Schema enforcement via a request parameter** (Chat Completions: `response_format: {type: "json_schema", ...}`; Responses API: `text.format`). It does **not** mean the provider cannot produce structured JSON.
>
> `AnthropicProvider` is marked `✗` because Anthropic's API has no equivalent enforcement parameter — when `output_schema` is set the loop clears it and falls back to text-based JSON parsing of the model's response. Anthropic **does** support tool use (`tool_choice = ✓`) and produces reliable structured output via the `final_tool_name` pattern (Path B — see §10).

**Choosing between the two OpenAI providers:**

| | `OpenAIChatCompletionsProvider` | `OpenAIResponsesProvider` |
|---|---|---|
| **API** | `POST /chat/completions` | `POST /responses` |
| **Compatible with** | Any OpenAI-compatible endpoint (DeepSeek, Azure, Groq, Together, …) | OpenAI only |
| **History** | Full message array resent every turn | Stateful — sends `previous_response_id`; only new messages travel the wire |
| **Reasoning/thinking** | `delta.reasoning_content` (DeepSeek-style extension) | Native `reasoning` object with `effort` + `summary` levels; encrypted reasoning tokens |
| **Structured output param** | `response_format: json_schema` | `text.format: json_schema` |
| **Use when** | Any OpenAI-compatible provider, or when `reasoning_content` round-trip is enough | OpenAI o1/o3/o4 and reasoning-native models where `effort` tuning matters |

Duck-typed test fakes that omit `capabilities()` are safely skipped via a `hasattr` guard — no test changes required when adding new providers.

`LlamaCppProvider` is a Chat Completions variant for llama.cpp server. It keeps
streaming enabled with `stream: true`, omits OpenAI's `stream_options` field,
and maps structured output to llama.cpp's documented `response_format` shape.

---

### 3.4 Context Building Pipeline

Context builders fire before every provider call, injecting ephemeral context without mutating conversation history.

```mermaid
flowchart TD
    SNAP["session.provider_view\n(immutable snapshot)"]

    subgraph Chain["ContextBuilderChain (registration order)"]
        direction TB
        B1["Builder 1\ne.g. MemoryContextBuilder"]
        B2["Builder 2\ne.g. custom RAG"]
        BN["Builder N"]
        B1 --> B2 --> BN
    end

    CBR["ContextBuildResult\nsystem_blocks · messages\nselected_tools · budget · metadata"]

    BUDGET["apply_context_budget()\ntrim messages and blocks\nto budget.max_tokens"]

    MERGE["Merge into ProviderRequest only\nnever appended to provider_view"]

    EVENT["yield ContextBuildEvent\n(block count · tool count · budget)"]

    SNAP --> Chain --> CBR --> BUDGET --> MERGE --> EVENT
```

**Rules:**
- Builder output is **ephemeral** — appended only to `ProviderRequest`, never to `session.provider_view` or `full_history`.
- `ContextBudget(max_tokens=N)` trims messages and system blocks before the request is sent.
- `selected_tools` narrows the provider schema list for this turn only; `session.agent.tools` is not mutated.
- Multiple builders compose via `ContextBuilderChain`; each receives the same unmodified view snapshot.
- Builders must not block — use `await` for I/O.

---

### 3.5 Session History Model

Two separate lists track conversation history; only one is ever sent to the LLM.

```mermaid
graph TD
    subgraph FH["full_history  — append-only audit record"]
        direction LR
        fh1["user turn 1"] --> fh2["assistant turn 1"] --> fh3["user turn 2"] --> fh4["assistant tool call"] --> fh5["tool result"] --> fh6["...all turns intact"]
    end

    subgraph PV["provider_view  — trimmed copy sent to LLM"]
        direction LR
        pvc["COMPACTED SUMMARY"] --> pv4["assistant tool call"] --> pv5["tool result"] --> pv6["...recent turns"]
    end

    COMP["Compaction\nmutates provider_view only\nreplaces old messages with summary\nnever touches full_history"]

    CTX["ContextBuilder output\nephemeral per-request\nappended to ProviderRequest only\nnot stored in either list"]

    COMP --> PV
    CTX -.->|"injected per-request"| PV
```

**Invariant:** `full_history` is a strict superset of the logical conversation. Do not write to it outside `loop.py`.

---

### 3.6 Permission Evaluation

Every tool call passes through the permission engine before reaching the scheduler.

```mermaid
flowchart TD
    CALLS["Pending tool calls"]

    RULES["Evaluate rule list in order\n1  ToolRule(tool_name, allow|deny)\n2  PathRule(path_globs, allow|deny)\n3  BashRule(cmd_patterns, allow|deny)"]

    MATCH{{"Rule\nmatched?"}}

    MODE{{"Mode\ndefault"}}

    AS["skip-dangerous\nauto-approve all"]
    AE["acceptEdits\nauto-approve file ops\nask for Bash"]
    ASK["emit PermissionRequestEvent\nsuspend loop\nwait for caller response"]

    EXEC["Dispatch to Scheduler"]
    DENY["ToolCallEndEvent(is_error=True)"]

    CALLS --> RULES --> MATCH
    MATCH -->|"allow"| EXEC
    MATCH -->|"deny"| DENY
    MATCH -->|"no match"| MODE
    MODE -->|"skip-dangerous"| AS --> EXEC
    MODE -->|"acceptEdits"| AE --> EXEC
    MODE -->|"default"| ASK
    ASK -->|"approved"| EXEC
    ASK -->|"denied"| DENY
```

---

### 3.7 Memory and RAG Layer

Core ships a pluggable protocol with in-memory and durable reference implementations. Vector databases, embedding clients, and graph stores are host-owned and inject via the same protocol.

```mermaid
graph TD
    subgraph Protocol["MemoryStore Protocol"]
        MS["MemoryStore\nasync search(query, limit, namespace)\nasync upsert(items: list[MemoryItem])"]
    end

    subgraph Ref["Reference Implementations"]
        IK["InMemoryKeywordMemoryStore\ncooperative keyword matching"]
        SQ["SqliteMemoryStore\nasync via single-worker executor\n(storage._executor.SqliteExecutor)"]
        PG["PostgresMemoryStore\noptional asyncpg backend\nlinch[postgres]"]
    end

    subgraph Host["Host-Owned Adapters"]
        VEC["Vector store\nembed + ANN search"]
        GRF["Graph store"]
        REM["Remote store\nAPI call"]
    end

    subgraph Integration["Integration Points"]
        MCB["MemoryContextBuilder\nrecalls top-K items\nas ephemeral context per turn"]
        MST["MemorySearchTool\nscope=read · parallel=True\nResourceAccess(memory:ns, read)"]
        MUT["MemoryUpsertTool\nscope=write\nResourceAccess(memory:ns, write)"]
    end

    MS --> IK & SQ
    MS --> VEC & GRF & REM
    IK & SQ & VEC & GRF & REM --> MCB
    IK & SQ & VEC & GRF & REM --> MST & MUT
```

Do not add vector database or embedding dependencies to core; adapters implement the protocol and live in examples.

---

### 3.8 Virtual Filesystem and Large-Result Offloading

Variable-length tool results (RAG, web search, large file reads) are the primary
cause of context-window blowup. The filesystem subsystem mirrors the Deep Agents
`FilesystemMiddleware` pattern: when a tool result exceeds a token threshold, the
scheduler writes the full payload to a `FileBackend` and substitutes a short
preview + path reference in `provider_view`. The model reads back only the slices
it needs via the `read_file` tool.

```mermaid
flowchart TD
    EXEC["tool.execute() → ToolResult\ncontent = full payload (potentially huge)"]

    OFFLOAD{{"offload enabled (on by default)\nAND backend attached\nAND tokens(content) > threshold\n(threshold = context_window × 0.1)"}}

    WRITE["backend.write(path, content)\nwrite full payload to FileBackend"]
    REPLACE["result.content = preview (N lines) + path hint\nresult.truncated = True\nresult.metadata[offloaded_to] = path"]
    PASSTHROUGH["result unchanged"]

    BLOCK["ToolResultBlock(content=preview)\nenters provider_view / full_history"]
    EVENT["ToolCallEndEvent\nresult = preview string\ntool_result = full ToolResult (for observers)"]

    EXEC --> OFFLOAD
    OFFLOAD -->|"yes"| WRITE --> REPLACE --> BLOCK
    OFFLOAD -->|"no"| PASSTHROUGH --> BLOCK
    BLOCK --> EVENT
```

**Backends** — all implement the same `FileBackend` protocol:

| Backend | Storage | Lifecycle | Use when |
|---|---|---|---|
| `StateFileBackend` | In-memory dict | Per-session (default) | Zero-overhead ephemeral scratch |
| `DiskFileBackend` | Real files under a root dir | Until deleted | Want human-inspectable files; root defaults to `.linch/offload` (gitignored) |
| `SqliteFileBackend` | SQLite table | Persistent across sessions | Need cross-session recall (e.g. `/memories/`) |
| `CompositeFileBackend` | Routes by path prefix | Mixed | Ephemeral scratch + persistent `/memories/` subtree |

**`FileBackend` protocol** — five async methods:

```python
class FileBackend(Protocol):
    async def read(self, path, *, offset=0, limit=None) -> str: ...
    async def write(self, path, content) -> None: ...
    async def ls(self, prefix="") -> list[str]: ...
    async def edit(self, path, old, new, *, replace_all=False) -> int: ...
    async def exists(self, path) -> bool: ...
    async def delete(self, path) -> None: ...
```

**Four tools** are registered automatically when a backend is configured:

| Tool | Scope | Description |
|---|---|---|
| `ls` | read | List virtual files, optionally filtered by prefix |
| `read_file` | read | Read a file with optional offset/limit line window |
| `write_file` | write | Write or overwrite a scratchpad file |
| `edit_file` | write | Exact-string replace within a file |

**Invariant:** offloading mutates only `ToolResult.content` before the
`ToolResultBlock` is built. The full `ToolResult` still rides on
`ToolCallEndEvent.tool_result` for observers. `full_history` contains the preview,
not the raw payload — matching the session's context budget.

---

## 4. Event Taxonomy

All events are `@dataclass(slots=True)` with a `type: Literal[...]` discriminator. Every cross-cutting concern surfaces through events; callers never poll internal state.

```mermaid
graph LR
    subgraph Life["Lifecycle"]
        direction TB
        SE["SystemEvent\ntype=system · subtype=init\nsession_id · run_id · model · tools · cwd"]
        UE["UserEvent\ntype=user · message"]
        AE["AssistantEvent\ntype=assistant · message · stop_reason"]
        PAE["PartialAssistantEvent\ntype=partial_assistant · delta"]
        RE["ResultEvent\ntype=result\nsubtype = success | error | aborted\nfinal_text · structured_output · total_usage"]
        ERR["ErrorEvent\ntype=error · error dict"]
    end

    subgraph Tools["Tool Execution"]
        direction TB
        TCS["ToolCallStartEvent\ntype=tool_call_start\ntool_name · input · summary"]
        TCE["ToolCallEndEvent\ntype=tool_call_end\nresult · is_error · duration_ms"]
        PRE["PermissionRequestEvent\ntype=permission_request"]
    end

    subgraph Ctx["Context & Control"]
        direction TB
        CBE["ContextBuildEvent\ntype=context_build\nblock counts · budget · metadata"]
        CE["CompactionEvent\ntype=compaction\nmessages before/after · tokens before/after"]
        LGE["LoopGuardEvent\ntype=loop_guard\nreason · detail · action"]
        UGE["UsageEvent\ntype=usage · usage · cumulative"]
    end

    subgraph Skill["Skills & Subagents"]
        direction TB
        SLE["SkillsLoadedEvent"]
        SIE["SkillInvokedEvent"]
        SCE["SkillCompletedEvent"]
        SAE["SubagentEvent\nwraps a nested Event"]
    end
```

`event_to_dict` and `event_from_dict` in `events.py` provide full round-trip serialization for all event types.

---

## 5. Key Data Types

```mermaid
classDiagram
    class Agent {
        +model: str
        +provider: BaseProvider
        +tools: ToolRegistry
        +loop_guard: LoopGuard | None
        +context_builder: ContextBuilder | None
        +max_turns: float
        +permission_engine: PermissionEngine
        +deps: Any
        +output_schema: OutputSchema | None
        +session() Session
    }

    class ProviderRequest {
        +model: str
        +system: list~SystemBlock~
        +tools: list~dict~
        +messages: list~Message~
        +output_schema: OutputSchema | None
        +tool_choice: ToolChoice | None
        +cache_prompt: bool | None
        +cache_ttl: str | None
        +max_output_tokens: int | None
    }

    class ProviderCapabilities {
        +context_window: int
        +parallel_tool_calls: bool
        +structured_output: bool
        +tool_choice: bool
        +prompt_cache: bool
    }

    class LoopGuard {
        +max_identical_tool_calls: int
        +max_consecutive_failures: int
        +force_final_answer: bool
    }

    class ToolResult {
        +content: str
        +summary: str | None
        +is_error: bool
        +metadata: dict | None
        +citations: list~Citation~
        +duration_ms: int
        +truncated: bool
    }

    class ContextBuildResult {
        +system_blocks: list~SystemBlock~
        +messages: list~Message~
        +selected_tools: Any
        +budget: ContextBudget
        +metadata: dict
    }

    class MemoryItem {
        +id: str
        +text: str
        +namespace: str | None
        +metadata: dict
    }

    class ResourceAccess {
        +resource: str
        +mode: Literal~read, write~
    }

    Agent --> LoopGuard
    Agent --> ProviderCapabilities : via provider.capabilities()
    Agent --> ProviderRequest : builds per turn
    ProviderRequest --> ProviderCapabilities : downgraded by
    ToolResult --> ResourceAccess : tool declares
```

---

## 6. Module Inventory

| Module | Responsibility |
|--------|---------------|
| `agent.py` | Immutable config; system block assembly; `session()` factory |
| `session.py` | Per-conversation state: `provider_view`, `full_history`, `run_deps`, `RunOptions` |
| `loop.py` | Turn orchestration, event emission, compaction trigger, loop guard wiring, capability downgrade |
| `types.py` | Shared dataclasses: `Message`, `ContentBlock`, `ProviderRequest`, `OutputSchema` |
| `events.py` | All event dataclasses + round-trip serialization (`event_to_dict` / `event_from_dict`) |
| `config.py` | `FeatureFlags`, `SystemPromptConfig` |
| `context/` | `ContextBuilder`, `ContextBuildResult`, `ContextBudget`, `ContextBuilderChain` |
| `loop_guard/` | `LoopGuard`, `LoopGuardState`, `LoopGuardDecision`, `evaluate_loop_guard`, `normalize_loop_guard` |
| `memory/` | `MemoryStore` protocol, reference stores, `MemoryContextBuilder`, memory tools |
| `filesystem/` | `FileBackend` protocol, `StateFileBackend`, `DiskFileBackend`, `SqliteFileBackend`, `CompositeFileBackend`, `OffloadConfig`, ls/read_file/write_file/edit_file tools |
| `scheduler.py` | Resource-aware parallel tool execution with concurrency cap; applies `maybe_offload` at the result chokepoint |
| `compaction.py` | Context-window management; calls `agent.provider` directly |
| `permissions/` | `PermissionEngine`: rule evaluation, event emission, loop suspension |
| `providers/` | `BaseProvider`, `ProviderCapabilities`; three implementations: `OpenAIChatCompletionsProvider` (any OpenAI-compatible endpoint, `reasoning_content` round-trip for DeepSeek/o-series), `OpenAIResponsesProvider` (stateful, native reasoning effort/summary), `AnthropicProvider` (extended thinking with signature, prompt caching) |
| `tools/` | Tool protocol, `ToolContext`, `ToolRegistry`, `ToolResult`, `Citation`, built-in tools |
| `sessions/` | `SessionStore` protocol, `InMemorySessionStore`, `SqliteSessionStore` |
| `mcp/` | MCP server connection → Linch tool adapters |
| `skills/` | `SKILL.md`-based slash-commands with argument substitution |
| `subagents/` | Specialized agent roles from `.linch/agents.yaml` |
| `recipes/` | *(removed)* — use `Agent(...)` directly; see `examples/` for domain patterns |

---

## 7. Provider Contract

Every provider implements `BaseProvider` (three methods):

```python
class BaseProvider(ABC):
    id: str

    def context_window(self, model: str) -> int: ...

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]: ...

    def capabilities(self, model: str) -> ProviderCapabilities:
        # Default derives context_window only; override to declare full support
        return ProviderCapabilities(context_window=self.context_window(model))
```

`stream()` yields **normalized dicts** — never raw API objects. Required keys by event type:

| `type` value | Required additional keys |
|---|---|
| `"message_start"` | `model: str` |
| `"text_delta"` | `text: str` |
| `"tool_use_start"` | `id: str`, `name: str` |
| `"tool_use_input_delta"` | `id: str`, `json_delta: str` |
| `"tool_use_end"` | `id: str` |
| `"thinking_delta"` | `text: str`, `signature?: str` |
| `"message_end"` | `stop_reason: StopReason`, `usage: Usage`, `provider_metadata: Any` |

The loop assembles these — it never imports any provider's raw types. Adding a new provider means implementing this dict contract only.

---

## 8. Tool Protocol

Tools are **duck-typed** — no base class, no `isinstance` check anywhere in the core:

```python
class MyTool:
    name: str                                      # unique registry key
    description: str                               # shown to the model
    input_schema: dict                             # JSON Schema object
    scope: Literal["read", "write", "exec"]
    parallel: bool                                 # V2 concurrency flag

    # Optional Phase-11 reliability attributes (all duck-typed via getattr)
    execution_timeout_ms: float                    # per-tool timeout; 0 = opt-out
    retryable: bool                                # opt write/exec tool into retry

    def validate(self, raw: dict) -> dict: ...
    def resources(self, input: dict) -> list[ResourceAccess]: ...
    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult: ...
    def summarize(self, input: dict) -> str: ...   # one-line for logs
```

`ToolContext` carries: `cwd`, `session_id`, `run_id`, `session_store`, `signal` (abort), `file_read_tracker`, `deps`, `filesystem`.

`deps` is threaded from `Agent(deps=...)` or overridden per-run with `RunOptions(deps=...)`. Use it to inject app state into tools without globals.

### ToolRegistry

```python
registry.add(tool)                        # add; raises if name exists
registry.remove(name)                     # remove by name
registry.replace(tool)                    # swap same-named tool
registry.select(names={...}, tags={...})  # runtime subset (per-request)
registry.copy()                           # shallow clone
registry.schemas()                        # provider-ready schema list
empty_tools(*extra)                       # no built-ins + optional extras
tools_from_defaults(exclude, extra)       # standard set ± named tools
```

---

## 9. System Prompt Layers

`Agent._build_system_blocks(tool_names)` assembles the system prompt from
ordered layers. `SystemPromptConfig.sections` can insert named reusable
sections before defaults, after defaults, or after the environment block without
changing the built-in prompt text:

```mermaid
flowchart TD
    L0["Layer 0 — before_defaults sections\nSystemPromptConfig.sections"]
    L1["Layer 1 — Custom blocks\nSystemPromptConfig.blocks\nprepended before identity"]
    L2["Layer 2 — Identity block\nYou are Linch…\nomitted when replace_defaults=True"]
    L3["Layer 3 — Protocol block\ntool-use instructions\nomitted when replace_defaults=True\nor no SWE tools present\nclauses conditional on registered tool families"]
    LFS["Layer 3b — Filesystem block\nadded when ls/read_file/write_file/edit_file are registered\nexplains virtual filesystem and offload recovery\npresent in both default and replace_defaults modes"]
    L4["Layer 4 — after_defaults sections\nSystemPromptConfig.sections"]
    L5["Layer 5 — Environment block\nalways present"]
    L6["Layer 6 — after_env sections\nSystemPromptConfig.sections"]
    L7["Layer 7 — Append block\nSystemPromptConfig.append\nor Agent(system_prompt=...)"]

    L0 --> L1 --> L2 --> L3 --> LFS --> L4 --> L5 --> L6 --> L7
```

**Invariant:** when the full default toolset is registered and `replace_defaults=False`, the protocol block is byte-identical to the pinned reference in `tests/test_system_blocks.py`. Change the wording only intentionally and update the parity test.

---

## 10. Structured Output Paths

Two independent mechanisms surface the same field: `ResultEvent.structured_output: dict | None`.

```mermaid
flowchart LR
    subgraph A["Path A — Text Parse\noutput_schema on Agent or RunOptions"]
        direction TB
        A1["Provider emits JSON text\nvia response_format or text.format"]
        A2["_parse_structured_output(final_text, schema)\noptional jsonschema validation"]
        A3["ResultEvent.structured_output = parsed\nResultEvent.structured_error = msg | None"]
        A1 --> A2 --> A3
    end

    subgraph B["Path B — Forced Tool\nfinal_tool_name on Agent or RunOptions"]
        direction TB
        B1["Model calls final_tool_name\nstop_reason = tool_use"]
        B2["loop.py intercepts ToolUseBlock\nbefore scheduler — tool is NOT executed"]
        B3["ResultEvent.structured_output = block.input"]
        B1 --> B2 --> B3
    end
```

Path B is more reliable for complex schemas and works across all providers without `response_format` support.

---

## 11. Compaction

`maybe_compact(session, agent)` is called at the top of each turn:

1. Count tokens in `provider_view` via `agent.provider.context_window(agent.model)`.
2. If within threshold — no-op.
3. Otherwise submit a summarization request via `agent.provider.stream()` and replace old messages in `provider_view` with the summary, emitting `CompactionEvent`.

**Invariant:** `full_history` is never modified. Only `provider_view` shrinks. Compaction uses the configured `agent.provider` — never a hardcoded OpenAI call.

`DefaultCompaction` remains the default. `DetailedCompaction` is opt-in via
`Agent(compaction=DetailedCompaction())` and uses a continuation-safe summary
with user intent, artifacts/files/code touched, errors/fixes, pending tasks,
current work, and the next step.

---

## 12. Skills and Subagents

**Skills** are loaded from `.linch/skills/*/SKILL.md`; built-in skills
such as `verify` are also registered unless a disk skill uses the same name.
Each file has YAML frontmatter (`name`, `description`, `allowed_tools`,
`model_override`) and a markdown body. When a skill is invoked, the body is
injected as a `<system-reminder>` per-turn via `_re_inject_skill_context`.
Gated by `FeatureFlags(skills=True)`.

**Subagents** are defined in `.linch/agents/*.md`; built-in named agents
such as `verification` are also registered unless a disk agent uses the same
name. `subagents/runner.py` creates a child agent with its own tool overlay and
system prompt. The child's system blocks are computed from its own tool names —
not copied from the parent. Gated by `FeatureFlags(subagents=True)`.

**MCP** — `connect_mcp_servers(configs)` wraps each MCP tool as a duck-typed Linch tool. Names are normalized via `mcp/naming.py`. The connection closes on `agent.close()`. Gated by `FeatureFlags(mcp=True)`.

---

## 13. Key Invariants

These must not break across refactors:

| # | Invariant |
|---|---|
| 1 | **`full_history` is append-only** — only `loop.py` appends; never write to it elsewhere. |
| 2 | **`provider_view` is the only thing compaction mutates** — `full_history` is untouched. |
| 3 | **Tool protocol is duck-typed** — no base class, no `isinstance`; check attribute presence. |
| 4 | **`stream()` yields normalized dicts** — the loop must not import any provider's raw types. |
| 5 | **Default SWE system-block text is pinned** — `test_system_blocks.py` has a byte-identical parity assertion; update it intentionally. |
| 6 | **`final_tool_name` tool is never scheduled** — the loop intercepts before the scheduler. |
| 7 | **Context builders do not mutate history** — they receive a `provider_view` snapshot and return ephemeral request context. |
| 8 | **`run_deps` is set once per `run_loop` call** — at the top, from `opts.deps ?? agent.deps`. |
| 9 | **Loop guard is on by default** — `Agent()` without `loop_guard=` gets `LoopGuard()` with safe thresholds; disable explicitly with `Agent(loop_guard=None)`. |
| 10 | **Provider capabilities apply per-request** — `_build_turn_request()` always calls `apply_provider_capabilities()` when the provider has `capabilities()`; no provider receives features it declared unsupported. |
| 11 | **Offload only replaces `ToolResult.content` before block construction** — the full result is preserved on `ToolCallEndEvent.tool_result`; `full_history` and `provider_view` receive the preview only. `maybe_offload` never raises — a backend write failure silently returns the original result so a storage hiccup never breaks a run. |
| 12 | **Filesystem tools are excluded from offloading** — `read_file`, `write_file`, `edit_file`, `ls` are in `OffloadConfig.skip_tools` by default; reading a large file back cannot trigger a recursive re-offload. |

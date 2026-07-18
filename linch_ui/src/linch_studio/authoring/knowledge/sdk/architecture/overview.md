# System Overview

> Part of the [Linch architecture guide](./README.md).

The framework is a harness of pluggable subsystems composed around a single event-driven loop. The caller only interacts with `Agent` (config) and `Session` (state); everything else is internal machinery wired together inside `run_loop`.

```mermaid
graph TD
    subgraph Host["Host Application"]
        Caller["Caller"]
    end

    subgraph Core["Linch Core"]
        Agent["Agent\nmodel · provider · tools\npermissions · loop_guard\nhooks · deps"]
        Session["Session\nprovider_view · full_history\nrun_deps · active_run_id"]
        RunLoop["run_loop()"]

        subgraph Pipeline["Turn Pipeline"]
            SK["_re_inject_skill_context()"]
            CB["ContextInjectionHook\nRAG · budget · tool select"]
            BTR["_build_turn_request()\n+ capability downgrade"]
            PERM["PermissionEngine\nrule eval · loop pause"]
            SCHED["Scheduler\nparallel · serialize · resource lock"]
            LG["LoopGuard\nloop detection"]
        end

        subgraph Providers["Providers"]
            OAR["OpenAIResponsesProvider"]
            OAC["OpenAIChatCompletionsProvider"]
            ANT["AnthropicProvider"]
            GEM["GeminiProvider"]
            LLM["LlamaCppProvider"]
        end

        subgraph Storage["Storage"]
            SS["SessionStore\nInMemory · SQLite"]
            RS["RunStore\nInMemory · SQLite"]
        end

        subgraph Knowledge["Knowledge"]
            MEM["MemoryStore\nkeyword · sqlite · postgres · tiered · custom"]
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
    BTR --> OAR & OAC & ANT & GEM & LLM
    RunLoop --> PERM
    PERM --> SCHED
    SCHED --> LG
    RunLoop -.->|"AsyncIterator[Event]"| Caller
    Session <-->|"persist / load"| SS
    RunLoop <-->|"checkpoint / resume"| RS
    CB <-->|"recall / upsert"| MEM
    Agent <--> MCP & SKILLS & SUBA
    SCHED <-->|"offload / read"| FS
    OFF -.->|"config"| SCHED
```

## Design rationale

- **One loop, pluggable subsystems.** All orchestration lives in `run_loop`; every
  subsystem (provider, store, memory, filesystem, permissions) is a duck-typed
  protocol wired in by reference. There is exactly one place control flow happens, and
  each subsystem is swappable without touching the loop.
- **The caller's surface is just `Agent` + `Session`.** `Agent` is immutable config,
  `Session` is mutable state; everything else is internal. A small public surface keeps
  the SDK embeddable and lets internals change without breaking callers (the supported
  surface is exactly `linch.__all__`).
- **Async-first, no blocking I/O in the core.** The whole loop is `async`, so a host
  app can run many agents concurrently and stream events without threads.
- **No process-global mutable state.** Each `Agent` builds its own registry, sessions,
  permission engine, and extension state — so N agents run in one process (multi-tenant)
  with no cross-talk.
- **Provider-agnostic by contract.** The loop only knows the `BaseProvider` interface;
  per-provider quirks are isolated behind `capabilities()` + a request downgrade, so the
  same agent code runs on OpenAI, Anthropic, Gemini, or a local model.

---

Back to the [architecture index](./README.md).

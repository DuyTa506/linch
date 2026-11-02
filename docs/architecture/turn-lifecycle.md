# Turn Lifecycle

> Part of the [Linch architecture guide](./README.md).

One complete agent turn — from receiving the prompt to deciding whether to continue looping.

```mermaid
sequenceDiagram
    actor Caller
    participant RL as run_loop()
    participant SK as _re_inject_skill_context()
    participant CB as ContextInjectionHook
    participant PR as Provider
    participant PE as PermissionEngine
    participant SC as Scheduler
    participant LG as LoopGuard

    Caller->>RL: session.run(prompt, RunOptions?)
    RL-->>Caller: SystemEvent
    RL-->>Caller: UserEvent

    loop Each turn
        RL->>SK: inject skill context (if skill active)
        Note over RL: sync_catalog persists prefix or tail reminders<br/>after completed tool batches; native existing-snapshot sync<br/>waits for context selection
        RL->>CB: build_context(session, turn_index)
        CB-->>RL: ContextBuildResult
        RL-->>Caller: ContextBuildEvent

        Note over RL: assemble ProviderRequest<br/>+ apply_provider_capabilities() (capability downgrade)

        RL->>PR: stream(ProviderRequest)
        PR-->>Caller: PartialAssistantEvent (streaming)
        PR-->>RL: AssistantAssembly(message, stop_reason, usage)

        RL-->>Caller: AssistantEvent + UsageEvent

        alt stop_reason = end_turn
            RL-->>Caller: ResultEvent(success)
        else stop_reason = tool_use
            RL->>SC: resolve + validate tool calls
            SC->>SC: enforce offered-tool boundary
            SC->>SC: PreToolUse transform/block/resolve
            SC->>SC: revalidate canonical input
            SC->>PE: evaluate final permissions
            PE-->>Caller: PermissionRequestEvent (if pending)
            PE-->>SC: approved or denied canonical calls

            SC-->>Caller: ToolCallStartEvent x N
            SC-->>Caller: ToolProgressEvent (optional, stream-only)
            Note over SC: when FeatureFlags(filesystem=True) enables it,<br/>maybe_offload() writes results over the configured threshold<br/>to FileBackend and replaces provider-facing content with a preview
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

## Typed lifecycle points

Catalog synchronization uses the tool registry generation and the agent catalog
revision as its unchanged fast path. Notifications are appended as user messages
containing an internal `CatalogChangeBlock`, wrapped in `<system-reminder>` tags,
before updating last-notified metadata. A crash between these writes can repeat a
reminder but cannot lose it. Notification text stays user-role on all providers;
opt-in positional adapters also render native tool structures as described in the
[provider contract](./provider-contract.md#native-catalog-rendering). Schema changes
use the existing `tool_set_changed` advisory. With native support enabled and an
existing snapshot, synchronization waits until context selection establishes the
offered-tool boundary; initial snapshot creation and option-off ordering remain
before context building. Both normal and reactive-compaction paths use the effective
per-turn model, including skill overrides.
Synchronization compares the live catalog directly with persisted `notified`
digests, including skills. Compaction resets these digests to immutable `base`
without changing the request prefix, so the next turn consolidates changes.

The sequence is backed by the catalog in [events.md](./events.md). Run, turn,
provider, and security hooks use `hook` points such as `AgentStart`,
`UserPromptSubmit`, `TurnStart`, `BeforeProviderCall`, `PreToolUse`, and
`AgentStop`. `HookDispatcher` awaits them in registration order and keeps the
security-sensitive mutation and permission ordering explicit.

The three kernel tool points — `tools/pre-execute`, `tools/execute`, and
`tools/post-execute` — are waterfalls. A wrapper must await its `next` callback
to continue the chain. `turn-stopping` is a serial veto point used before a
candidate finalization; an accepting listener returns control to the normal
terminal path, while a continue or fail decision changes that path. The bus
rejects dispatch through a mode that does not match the point's contract.

`HookDispatcher` remains the user-hook adapter rather than becoming a generic
bus listener. It threads mutations across hooks, isolates hook failures, and
preserves the canonical `PreToolUse` validation and permission ordering.

## Design rationale

- **The model decides when to stop, not a step counter.** The loop continues while
  the response contains tool calls and stops on a text-only (`end_turn`) response.
  This lets a task take as many or as few turns as it needs; `max_turns` and the
  loop guard are safety bounds, not the primary control.
- **The security gate has a canonical order.** The scheduler resolves and validates the
  model proposal, enforces that the tool was offered in this request, runs `PreToolUse`,
  validates the transformed input again, and only then evaluates rules or asks the caller
  for approval. An unoffered call never reaches hooks; denied and unoffered calls never
  produce a side effect.
- **Context-builder output is appended to the request, never written into
  `provider_view`.** Per-turn RAG/memory is ephemeral: it informs one provider call
  without polluting the durable conversation, which keeps `provider_view` stable and
  replayable across turns.
- **Everything is an event over an async generator.** The caller drives iteration, so
  a slow consumer naturally backpressures the producer, and a UI can render
  streaming/usage/permission/progress events as they arrive instead of waiting for the
  turn to finish. `ToolProgressEvent` is observational only: it is neither provider
  history nor durable tool-result state.
- **Loop-guard and optional offload run at fixed chokepoints.** Loop detection evaluates
  each tool batch (no extra LLM call), and filesystem-enabled `maybe_offload` is applied
  at the single result chokepoint — both are structural, so an enabled path cannot be
  skipped by a code path that forgets to call them.

---

Back to the [architecture index](./README.md).

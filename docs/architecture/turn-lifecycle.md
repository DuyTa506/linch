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
            SC->>SC: PreToolUse transform/block/resolve
            SC->>SC: revalidate canonical input<br/>enforce offered-tool boundary
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

## Design rationale

- **The model decides when to stop, not a step counter.** The loop continues while
  the response contains tool calls and stops on a text-only (`end_turn`) response.
  This lets a task take as many or as few turns as it needs; `max_turns` and the
  loop guard are safety bounds, not the primary control.
- **The security gate has a canonical order.** The scheduler resolves and validates the
  model proposal, runs `PreToolUse`, validates the transformed input again, enforces
  that the tool was offered in this request, and only then evaluates rules or asks the
  caller for approval. A denied or unoffered call never produces a side effect.
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

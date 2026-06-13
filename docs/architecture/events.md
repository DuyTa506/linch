# Event Taxonomy

> Part of the [Linch architecture guide](./README.md).

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
        BE["BudgetEvent\ntype=budget · kind = warning | exceeded\nspent/max tokens · spent/max USD"]
    end

    subgraph Skill["Skills & Subagents"]
        direction TB
        SLE["SkillsLoadedEvent"]
        SIE["SkillInvokedEvent"]
        SCE["SkillCompletedEvent"]
        SAE["SubagentEvent\nwraps a nested Event"]
        BWE["BackgroundWorkerEvent\nworker_id · status · display_name"]
        WFE["WorkflowEvent\ntype=workflow\nkind = phase | agent_start | agent_end | agent_replayed"]
    end
```

`BackgroundWorkerEvent` is emitted when a background worker task completes (success or failure); it carries `worker_id`, `status`, and `display_name`.

`event_to_dict` and `event_from_dict` in `events.py` provide full round-trip serialization for all event types.

## Design rationale

- **Events are the *only* output channel.** Every cross-cutting concern (assistant
  text, tool calls, permissions, usage, compaction, budget, subagents) surfaces as an
  event; callers never poll internal state. This makes the loop observable and lets a
  UI render progress live without reaching into `Session`.
- **A `type` literal discriminator + `slots=True`.** The literal makes events
  cheap to switch on and safe to pattern-match; `slots` keeps them light since one run
  can emit thousands.
- **Full round-trip serialization (`event_to_dict`/`event_from_dict`).** Events are
  the persisted run log, so they must survive a process restart and reload — which is
  what makes durable resume and offline run reports possible.
- **Nesting via `SubagentEvent`, not a flattened stream.** A child's events are wrapped
  rather than merged inline, so a consumer can tell parent activity from worker activity
  and reconstruct the tree.

---

Back to the [architecture index](./README.md).

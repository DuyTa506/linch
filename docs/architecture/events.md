# Event Taxonomy

Use this page to inspect event types and lifecycle dispatch points.

> Part of the [Linch architecture guide](./README.md).

Event types are `@dataclass(slots=True)` objects with a `type: Literal[...]` discriminator. The diagram summarizes common stream events; journal and transient events have different persistence behavior.

```mermaid
graph LR
    subgraph Life["Lifecycle"]
        direction TB
        SE["SystemEvent\ntype=system · subtype=init\nsession_id · run_id · model · tools · cwd"]
        UE["UserEvent\ntype=user · message"]
        AE["AssistantEvent\ntype=assistant · message · stop_reason"]
        PAE["PartialAssistantEvent\ntype=partial_assistant · delta"]
        RE["ResultEvent\ntype=result\nsubtype = success | error | aborted | interrupted\nfinal_text · structured_output · total_usage"]
        ERR["ErrorEvent\ntype=error · error dict"]
    end

    subgraph Tools["Tool Execution"]
        direction TB
        TCS["ToolCallStartEvent\ntype=tool_call_start\ntool_name · input · summary"]
        TCE["ToolCallEndEvent\ntype=tool_call_end\nresult · is_error · duration_ms · tool_output?"]
        PRE["PermissionRequestEvent\ntype=permission_request"]
    end

    subgraph Ctx["Context & Control"]
        direction TB
        CBE["ContextBuildEvent\ntype=context_build\nblock counts · budget · metadata"]
        CE["CompactionEvent\ntype=compaction\nmessages before/after · tokens before/after"]
        LGE["LoopGuardEvent\ntype=loop_guard\nreason · detail · action"]
        UGE["UsageEvent\ntype=usage · usage · cumulative"]
        BE["BudgetEvent\ntype=budget · kind = warning | exceeded\nspent/max tokens · spent/max USD"]
        PCA["PromptCacheAdvisoryEvent\ntype=prompt_cache_advisory\nreason = tool_set_changed | model_changed · detail"]
    end

    subgraph Skill["Skills & Subagents"]
        direction TB
        SLE["SkillsLoadedEvent"]
        SIE["SkillInvokedEvent"]
        SCE["SkillCompletedEvent"]
        SAE["SubagentEvent\nwraps a nested Event"]
        BWE["BackgroundWorkerEvent\nworker_id · status · display_name"]
        WFE["WorkflowEvent\ntype=workflow\nkind = phase | agent_start/end/replayed | step_start/end/replayed\n| interrupt_requested/resolved/replayed\n(WORKFLOW_EVENT_KINDS in events.py)"]
    end
```

## Typed lifecycle catalog

The kernel lifecycle is defined once as typed points with an explicit dispatch
mode. The table below is generated from `linch.lifecycle` and source call sites;
run `python scripts/generate_event_catalog.py --check` to detect drift.

<!-- BEGIN GENERATED LIFECYCLE CATALOG -->
| Point | Payload | Mode | Producers | Consumers |
|---|---|---|---|---|
| `context/service` | `Context + key` | `emit` | `linch.kernel.context:Context._publish_plugin`<br>`linch.kernel.context:Context._withdraw_plugin`<br>`linch.kernel.context:Context.register`<br>`linch.kernel.context:Context.scope` | `linch.kernel.context:Context.scope`<br>`linch.kernel.plugins:_PluginMount.start` |
| `AgentStart` | `AgentStartContext` | `hook` | `linch.loop.startup:initialize_run` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `AgentStop` | `AgentStopContext` | `hook` | `linch.loop.runner:_run_loop_impl`<br>`linch.loop.startup:_emit_prompt_block` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `UserPromptSubmit` | `UserPromptSubmitContext` | `hook` | `linch.loop.dispatch:dispatch_user_prompt` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `TurnStart` | `TurnStartContext` | `hook` | `linch.loop.runtime:TurnLifecycle.start_turn` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `TurnStop` | `TurnStopContext` | `hook` | `linch.loop.runtime:TurnLifecycle.end_active_turn` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `BeforeProviderCall` | `BeforeProviderCallContext` | `hook` | `linch.loop.dispatch:dispatch_before_provider_call` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `ProviderCallStart` | `ProviderCallStartContext` | `hook` | `linch.loop.runtime:TurnLifecycle.start_provider_call` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `ProviderCallStop` | `ProviderCallStopContext` | `hook` | `linch.loop.runtime:TurnLifecycle.end_active_provider_call` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `AfterProviderCall` | `AfterProviderCallContext` | `hook` | `linch.loop.dispatch:dispatch_after_provider_call` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `PreToolUse` | `PreToolUseContext` | `hook` | `linch.scheduler:_dispatch_pre_tool_use` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `ToolUseStart` | `ToolUseStartContext` | `hook` | `linch.loop.tool_batch:run_tool_batch` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `ToolUseStop` | `ToolUseStopContext` | `hook` | `linch.loop.tool_batch:run_tool_batch` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `PostToolUse` | `PostToolUseContext` | `hook` | `linch.coordination.mailbox.__init__:_render_peer_message`<br>`linch.scheduler:_dispatch_post_tool_use`<br>`linch.tools.subagent:SubagentTool.execute` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `PostToolUseFailure` | `PostToolUseFailureContext` | `hook` | `linch.scheduler:_dispatch_post_tool_use_failure` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `PreCompact` | `PreCompactContext` | `hook` | `linch.compaction:_run_compaction_impl` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `PostCompact` | `PostCompactContext` | `hook` | `linch.compaction:_run_compaction_impl` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `BeforeFinalAnswer` | `BeforeFinalAnswerContext` | `hook` | `linch.loop.dispatch:dispatch_before_final_answer` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `Stop` | `StopContext` | `hook` | `linch.loop.dispatch:dispatch_stop` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `SubagentStart` | `SubagentStartContext` | `hook` | `linch.subagents.runner:_run_subagent_owned`<br>`linch.subagents.runner:continue_subagent` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `SubagentStop` | `SubagentStopContext` | `hook` | `linch.subagents.runner:_run_subagent_owned`<br>`linch.subagents.runner:continue_subagent` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `EventEmit` | `EventEmitContext` | `hook` | `linch.session:Session._iterate` | `linch.hooks.dispatcher:HookDispatcher.dispatch` |
| `tools/pre-execute` | `ToolExecution` | `waterfall` | `linch.tools.pipeline:ToolPipeline.run_pre_execute` | `linch.tools.pipeline:ToolPipeline.on_pre_execute` |
| `tools/execute` | `ToolExecution` | `waterfall` | `linch.tools.pipeline:ToolPipeline.run_execute` | `linch.tools.pipeline:ToolPipeline.on_execute` |
| `tools/post-execute` | `ToolExecution + result` | `waterfall` | `linch.tools.pipeline:ToolPipeline.run_post_execute` | `linch.tools.pipeline:ToolPipeline.on_post_execute` |
| `tools/resolve` | `str + dict + Session` | `serial` | `linch.scheduler:execute_tool_calls` | `linch.tools.search:ToolSearchPlugin.setup` |
| `turn-stopping` | `TurnStopping` | `serial` | `linch.loop.turn_stopping:TurnStoppingDispatcher.dispatch` | `linch.loop.turn_stopping:TurnStoppingDispatcher.register` |
<!-- END GENERATED LIFECYCLE CATALOG -->

`BackgroundWorkerEvent` is emitted when a background worker task completes (success or failure); it carries `worker_id`, `status`, and `display_name`.

`PromptCacheAdvisoryEvent` is emitted (observationally, never mutating the request) when the tool set or model changes between provider calls on the same session and invalidates the cached request prefix — see [prompt caching](../usage/providers.md#the-prompt_cache_advisory-event).

The event serializer supports event round trips. Tool progress is transient and is not stored in the durable run event log; model-call accounting events are journal-only.

`SystemEvent` and the terminal `ResultEvent` carry the run `trace_id` when a
run is persisted or streamed. The creation/origin value is retained in
`RunRecord.meta`, captured read-only by `ToolContext.trace_id`, and can be
attached to host log records with `linch.TraceIdFilter`. A valid active
OpenTelemetry context supplies the ID; runs without one use the stable run ID
as their correlation fallback. A resumed attempt follows its caller's active
trace, while a caller without one reuses that same durable run ID.

Readers are strict by default: an unknown or malformed required event — at the
top level or nested inside an event payload — is a load error, because silently
dropping it could change the meaning of a resumed run. Only an event explicitly
tagged `ignorable` may decode to the public `IgnorableEvent` sentinel and be
skipped by a consumer. Ignorability is a producer-declared compatibility
property, not a fallback for unknown required fields or nested shapes.

## Design rationale

- **Events make runs observable.** Assistant text, tool calls, permissions,
  usage, compaction, budget, and subagent activity can be consumed by a UI.
- **A `type` literal discriminator + `slots=True`.** The literal makes events
  cheap to switch on and safe to pattern-match; `slots` keeps them light since one run
  can emit thousands.
- **Full round-trip serialization (`event_to_dict`/`event_from_dict`).** Events are
  serializable for durable logs and offline reports. Not every stream event is
  persisted, and accounting records are journal-only.
- **Nesting via `SubagentEvent`, not a flattened stream.** A child's events are wrapped
  rather than merged inline, so a consumer can tell parent activity from worker activity
  and reconstruct the tree.

---

Back to the [architecture index](./README.md).

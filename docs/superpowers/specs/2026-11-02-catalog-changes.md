# Mid-session catalog changes that keep the prompt cache

Status: approved direction (2026-11-02), target 3.1. Phase 2 below is the
implementation contract. Phases 3 and 4 are outlines and need measurement results
from phase 1 before they become contracts.

## 1. Problem

A tool definition change invalidates `tools`, `system`, and `messages` on Anthropic
and alters the rendered prefix on OpenAI and on self-hosted servers. Linch 3.0 mutates
`tools` directly and rebuilds system blocks every request, so a reload costs the whole
session's cache.

Verified 3.0 defects this spec removes:

- `_prompt_message` inserts the skill listing into every run's prompt with no dedupe.
- There is no way to reload skills. `connect_skills()` runs once.
- `reload_subagents()` unregisters and re-registers, moving those tools to the end of
  the insertion order.
- `Tools available: ...` sits in a cacheable system block and follows the live registry.
- `ToolSearchPlugin` rewrites `tool_search.description` and the offered list as tools
  load, which changes `tools`.

## 2. Decision

The prefix is frozen per session. Later changes go at the tail of the conversation.

| Change | Positional providers (Anthropic, OpenAI Responses) | Append-only (Chat Completions, vLLM, llama.cpp, Gemini, others) |
|---|---|---|
| Skill, subagent list, text | Tail notification | Tail notification |
| Tool removed | `tool_removal`, or `allowed_tools` | Schema stays, notify, resolve fails |
| Tool added or `input_schema` edited | `tool_addition`, or `additional_tools` | Change `tools`, new tools at the end, accept the miss |
| Description-only edit | Tail notification | Tail notification |

Rejected: a `CallTool` gateway (revisit only if phase 1 shows prefill cost matters on
self-hosted servers), and delaying tool changes to cold-cache moments, because a new
tool must work on the next request.

## 3. Phases

1. Measure with `scripts/measure_catalog_changes.py`. Needs owner-provided keys or
   servers. Record results in `docs/superpowers/measurements/`.
2. Provider-neutral base. This section's contract. Usable alone.
3. Native adapters, gated by a `ProviderCapabilities` field.
4. `ToolSearchPlugin` returns `tool_reference` on Anthropic and stops changing `tools`.

## 4. Phase 2 contract

Line budget: about 500 lines of `src/`, excluding tests. No new dependency, no new
public name except `Agent.reload_skills`.

### 4.1 Snapshot

A session gets one snapshot, durable at `session.meta["linch.prefix"]`, written through
`await session.update_meta(...)` before its first provider request:

```python
{
    "version": 1,
    "system": [{"text": str, "cacheable": bool}, ...],  # after the skill block
    "tools": [{"name": str, "description": str, "input_schema": dict}, ...],  # wire order
    "skills": {name: digest},
    "notified": {
        "tools": {name: digest},
        "subagents": str | None,
    },
}
```

- `_build_turn_request` uses `snapshot["system"]` and `snapshot["tools"]` in place of
  freshly built values, then applies existing context selection, `allowed_tools`, and
  capability downgrades unchanged. Per-turn `ContextBuildResult.system_blocks` keep
  following the snapshot with `cacheable=False`.
- A worker resuming a session reads the snapshot and must produce byte-identical
  `system` and `tools`. It must not read OS, shell, or Python version from its host.
- A session without a snapshot gets one at its next run. That is the single expected
  cache miss on upgrade.

The snapshot is bypassed, and 3.0 behavior stays, when any of these holds:
`config._values is not None` (a profile overlay), `session.tools_override`, an effective
`system_blocks_override`, or an active `request_catalog`. Dedupe from 4.3 still applies.

Session stores have no lease in 3.0. Snapshot and `notified` writes carry the same
fencing as `session.append`, no weaker and no stronger. A zombie write can only cause
a repeated notification or one miss. Do not add a lease API here.

### 4.2 Skill block

With a snapshot, the skill listing is one cacheable `SystemBlock` placed after the env
block and before user-provided instructions. It is frozen with the rest and no longer
inserted in user prompts.

`loop/contracts.py` fingerprints `agent.skill_listing_text` as
`compaction_skill_listing_fingerprint`, and `compaction._skill_context_messages` reads
the same field. Both must use the session's snapshot when one exists. Otherwise
`reload_skills()` during a durable run fails contract validation on resume.

### 4.3 Dedupe

Without a snapshot, `_prompt_message` skips the listing when the exact reminder is
already in `session.provider_view`.

### 4.4 Change detection

Add one awaited step, `sync_catalog(session, config, lease)`, before context building
in `loop/provider_turn.py` and in the matching path in `loop/streaming.py`. At that
point the view ends in a user turn or tool results, so tool batches are complete.

- Fast path: compare `config.tools.generation` and a new `Agent._catalog_revision`
  with values held on the session. Equal means no work and no allocation.
- Otherwise compare the live catalog with the snapshot plus `notified`, using a
  canonical JSON SHA-256 per tool. `tool_signature`'s canonicalization is reusable.
- Reference skills by name and description only. Skill bodies never go in notifications.

| Detected | Action |
|---|---|
| Skill added, changed, removed | Notification text |
| Subagent description changed | Notification text |
| Tool description changed only | Notification text, snapshot unchanged, `notified` updated |
| Tool removed | Notification text, schema stays in the snapshot |
| Removed tool called anyway | Existing unknown-tool path returns the error |
| Tool added | Notification text, append to `snapshot["tools"]` |
| Tool `input_schema` changed | Notification text, replace in place in `snapshot["tools"]` |

Tool additions and schema edits change `tools`, so the existing `tool_set_changed`
advisory fires on its own. Do not add a new event.

### 4.5 Notification

Add `CatalogChangeBlock` in `types.py`:

```python
@dataclass(slots=True)
class CatalogChangeBlock:
    text: str
    added_tools: tuple[str, ...] = ()
    removed_tools: tuple[str, ...] = ()
    type: Literal["catalog_change"] = "catalog_change"
```

- Round-trip it through `message_to_dict` and `message_from_dict`.
- It is appended with `await session.append([...])` in a user-role message, wrapped by
  `wrap_in_system_reminder`. `SessionLog.append` alone is not durable.
- In phase 2 every adapter renders it as text. Audit every `isinstance(block, TextBlock)`
  site in `providers/`, `openai_responses.py`, compaction, and token estimators: a block
  that is silently dropped is a defect.
- MCP-provided descriptions and instructions are third-party text. The notification
  states the tool's name only. On positional providers this is required, because
  phase 3 renders notifications as system messages with operator authority.

Order: append the message, then update `notified`. A crash between the two repeats a
notification, which is harmless. The reverse order loses one, which is not.

### 4.6 Compaction

If `notified` differs from the snapshot's base after compaction, rebuild the snapshot
from the live catalog, reset `notified`, and persist before the next provider call. The
notifications are gone with the replaced view and the new prefix now contains their
content. If nothing changed, keep the snapshot so the cached prefix survives.

Both compaction paths call `_re_inject_skill_context`, so that is the seam. A crash after
the projection and before the snapshot write is recovered by 4.4 on the next turn, which
re-detects the difference.

### 4.7 Reload APIs

`Agent.reload_skills()` is async. It rescans off the event loop, then in one synchronous
step replaces `self.skills` contents, recomputes `skill_listing_text`, removes permission
rules it previously derived for skills, adds the new ones, and increments
`_catalog_revision`. It must not create a second `SkillTool` or a second `ToolRegistry`
entry. If no skill remains the `Skill` tool stays registered: unregistering it would be a
catalog change for no benefit.

`reload_subagents()` keeps each tool's slot through `replace`, so order survives, and
increments `_catalog_revision`. When the feature is off it keeps today's behavior.

## 5. Required tests

Write each failing first.

- Two runs in one session send exactly one skill listing.
- `system` and `tools` are byte-identical across turns while the catalog is stable.
- Same after a process restart with a different `platform` value patched in.
- `reload_skills()` mid-session: `tools` and `system` unchanged, one notification, model
  can invoke the new skill on the next request.
- Tool added on an append-only provider: earlier tools keep position, new tool last, one
  `tool_set_changed` advisory.
- Tool removed: `tools` unchanged, a call returns the unknown-tool error.
- Description-only change: `tools` unchanged.
- `reload_subagents()` with an MCP tool registered after `Subagent`: order unchanged.
- Notification survives reload from a session store.
- Compaction with pending changes: new snapshot contains them, no notification left in
  the view. Compaction with none: snapshot unchanged.
- Crash between message append and `notified` write: one repeat at most, never a loss.
- Durable resume after `reload_skills()` passes contract validation.
- Each bypass condition in 4.1 keeps 3.0 request bytes, except dedupe.
- An agent with no change executes no snapshot diff after the first turn.

## 6. Phase 3 outline

- Add `ProviderCapabilities.catalog_changes: Literal["append_only", "positional"]`,
  default `"append_only"`.
- Add `"system"` to `Message.role`, or render from `CatalogChangeBlock` in the Anthropic
  adapter only. Prefer the latter: other providers then need no change.
- Anthropic: `role: "system"` content with `text`, `tool_addition`, `tool_removal`, plus
  `inline-tools-2026-09-15`. Bedrock and Google Cloud get by-reference only, through
  `mid-conversation-tool-changes-2026-07-01`. Sonnet 5 and any model outside the docs list
  fall back to append-only.
- Placement: only after a user turn or tool results, never between `tool_use` and its
  result, and not right after a paused turn. A sent system message is never edited.
- Definitions sent after the first user message are capped at 4 MB. At least one
  non-deferred tool stays in `tools`.
- OpenAI Responses: `additional_tools` with role `developer`, keep the item in place on
  round-trips, restrict with `tool_choice.allowed_tools`.
- Compaction must promote native additions as in 4.6, or they vanish with the history.
- Test Sonnet 5.5 and Haiku 5.5 specifically: the docs warn they may distrust a system
  message after a tool result.

## 7. Out of scope

- Mapping per-turn `allowed_tools` and `selected_tools` narrowing to native restrictions.
- Making LLM compaction reuse the parent prefix. Worth a separate review.
- Replacing `prompt_cache_advisory` with Anthropic cache diagnostics.
- Profile overlays and `tools_override` sessions.

## 8. References

- https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool
- https://developers.openai.com/api/docs/guides/tools-tool-search
- https://developers.openai.com/api/docs/guides/prompt-caching
- https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/

# Mid-session catalog changes that keep the prompt cache

Status: approved direction (2026-11-02), target 3.0.1. Phases 2 and 3 below are
implementation contracts. Phase 4 is an outline. The owner chose a patch release even though
`docs/versioning.md` places public additions in MINOR; the changelog records that exception.

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
| Tool added | `tool_addition`, or `additional_tools` | Change `tools`, new tools at the end, accept the miss |
| `input_schema` edited | Change `tools`, accept the miss | Change `tools`, accept the miss |
| Description-only edit | Tail notification | Tail notification |

Rejected: a `CallTool` gateway (revisit only if phase 1 shows prefill cost matters on
self-hosted servers), and delaying tool changes to cold-cache moments, because a new
tool must work on the next request.

## 3. Phases

1. Measure with `scripts/measure_catalog_changes.py`. Needs owner-provided keys or
   servers. Record results in `docs/superpowers/measurements/`.
2. Provider-neutral base. This section's contract. Usable alone.
3. Native adapters, off by default, gated by a `ProviderCapabilities` field.
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
    "base": {"tools": {name: digest}, "skills": {name: digest}, "subagents": str | None},
    "notified": {"tools": {name: digest}, "skills": {name: digest}, "subagents": str | None},
}
```

`base` is what the frozen `system` text describes and never changes. `notified` is what the
model has been told so far. `tools` is the wire list: it grows or changes on append-only
providers, and it stays fixed on positional ones in phase 3.

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

Appending keeps earlier tools in place but limits the miss only on some templates.
Measured on 2026-11-02: DeepSeek keeps hits up to the change, vLLM and SGLang lose all of
them. Treat a tool change on those servers as a full miss.

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
- A description-only change must carry the new description, or the model learns nothing.
  This also applies to `Subagent`, whose description lists agent types.
- MCP-provided descriptions and instructions are third-party text. For an MCP tool the
  notification states the name only. Phase 3 keeps notification text in user-role messages
  on every provider, so it never gains operator authority.

Order: append the message, then update `notified`. A crash between the two repeats a
notification, which is harmless. The reverse order loses one, which is not.

### 4.6 Compaction

Compaction never changes `system` or `tools`. A changed prefix would discard a cache that
compaction leaves valid, and would break the durable run contract on resume.

After a compaction, when `notified` differs from `base`, set `notified` to a copy of `base`
and persist. Clear the session's fast-path values so the next `sync_catalog` runs. It then
emits one consolidated notification of everything that differs from `base`. Existing
tool rows stay in place, so this writes no `tools` change.

Crash recovery: at the start of a slow-path `sync_catalog`, if `notified` differs from
`base` and the provider view holds no `catalog_change` block, apply the same reset.

A new session picks up the live catalog in its own snapshot. That is the only way the
prefix absorbs changes.

### 4.7 Reload APIs

`Agent.reload_skills()` is async. It rescans off the event loop, then in one synchronous
step replaces `self.skills` contents, recomputes `skill_listing_text`, removes permission
rules it previously derived for skills, adds the new ones, and increments
`_catalog_revision`. It must not create a second `SkillTool` or a second `ToolRegistry`
entry. If no skill remains the `Skill` tool stays registered: unregistering it would be a
catalog change for no benefit.

`reload_subagents()` keeps each tool's slot through `replace`, so order survives, and
increments `_catalog_revision`. When the feature is off it keeps today's behavior.

Measurements: `docs/superpowers/measurements/2026-11-02-catalog-changes.md`.

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
- Compaction with pending changes: `system` and `tools` bytes unchanged, one consolidated
  notification follows. Compaction with none: no notification, no meta write.
- Resume after `reload_skills()`, a compaction, and a crash passes contract validation.
- A skill notification is not repeated after a restart or an unrelated tool change.
- A description-only change's notification contains the new description, except for MCP.
- Crash between message append and `notified` write: one repeat at most, never a loss.
- Durable resume after `reload_skills()` passes contract validation.
- Each bypass condition in 4.1 keeps 3.0 request bytes, except dedupe.
- An agent with no change executes no snapshot diff after the first turn.

## 6. Phase 3 contract

Line budget: about 200 lines of `src/`, excluding tests. No new dependency and no new name
in `linch.__all__`. Written from provider docs read on 2026-11-02. No key was available, so
this is unverified against live Anthropic and OpenAI endpoints. Docs must say so.

### 6.1 Switches

- Add `ProviderCapabilities.catalog_changes: Literal["append_only", "positional"]`, default
  `"append_only"`. `full_capabilities` keeps the default.
- Add `native_catalog_changes: bool = False` to `AnthropicProviderOptions` and
  `OpenAIResponsesProviderOptions`. With `False`, payloads stay byte-identical to phase 2.
- Anthropic reports `"positional"` only when the option is on, the wire mode is `"native"`
  (first-party endpoint), and the model is in an exact allowlist: `claude-fable-5-1`,
  `claude-mythos-5-1`, `claude-fable-5`, `claude-mythos-5`, `claude-opus-5-5`, `claude-opus-5`,
  `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-opus-4-8`. No prefix matching:
  `claude-sonnet-5` is unsupported and must stay append-only. Bedrock, Google Cloud, and
  proxies stay append-only, because by-value definitions need `inline-tools-2026-09-15`,
  which is Claude API only.
- OpenAI Responses reports `"positional"` only when the option is on and the model is
  `gpt-5.4` or later. The docs state no model list for `additional_tools`; this is the
  conservative bound from `tool_search`.

### 6.2 Core

Add `tool_definitions: tuple[dict[str, Any], ...] = ()` to `CatalogChangeBlock`, round-tripped.

`sync_catalog` reads the capability for the run's model. When `"positional"`, and
`snapshot["tools"]` is not empty, and the turn has no `allowed_tools` or context tool
selection narrowing:

| Detected | Action |
|---|---|
| Tool added, name not in `snapshot["tools"]` | Text, put definition in `tool_definitions`, name in `added_tools`, `snapshot["tools"]` unchanged |
| Tool restored, name in `snapshot["tools"]` | Text, name in `added_tools`, no definition |
| Tool removed | Text, name in `removed_tools`, snapshot unchanged |
| `input_schema` changed | Phase 2 path: replace the row, accept the miss |
| Description-only, skill, subagent | Phase 2 path |

The docs do not define redefining an existing name, so a schema edit is not sent natively.
Otherwise phase 2 behavior applies.

`_build_turn_request` sends `snapshot["tools"]` when positional. When not positional it sends
`snapshot_tools(snapshot, live_schemas)`, so a fallback model mid-session still sees tools that
were added natively. A later switch back keeps working, because the log still holds the block.

Compaction needs no new logic. 4.6 reset makes the next sync emit the differences from `base`.

### 6.3 Anthropic rendering

Text stays in the user-role message exactly as in phase 2. System messages carry only tool
blocks. That keeps operator authority away from notification text, avoids the documented
Sonnet 5.5 and Haiku 5.5 distrust of system text after tool results, and keeps the existing
cache breakpoint on the last user message. This replaces the earlier plan.

Only when `req` is positional, walk messages in order with a set of active names, seeded
from `req.tools`, and render as:

```json
{"role": "system", "content": [
  {"type": "tool_addition", "tool": {"type": "tool_definition", "definition":
    {"name": "x", "description": "...", "input_schema": {}}}},
  {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "y"}},
  {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "z"}}
]}
```

- Buffer tool blocks and emit one system message immediately before the next assistant
  message, or at the end of `messages`. That satisfies placement: it follows a user turn and
  precedes an assistant turn or ends the array, and it covers an aborted turn followed by a
  new prompt. Skip emission when it would be the first entry.
- Skip an addition whose name is active and a removal whose name is not active. This handles
  a block retained through compaction next to the consolidated one. Rendering must be
  deterministic from `messages` and `req.tools`, so sent messages never change.
- `tool_reference` is used only for names declared in `req.tools`, in additions and removals.
  The docs do not say a by-value name can be referenced, and an unresolved reference is a 400.
- A tool defined by value is never removed natively. Its removal is text only, it stays
  active on the wire, and a call gets the existing unknown-tool error, as in phase 2. If it
  is restored, the active-name check skips the addition.
- A by-value tool whose schema later changes takes the phase 2 path and enters `req.tools`.
  The seeded active set then skips its older addition. That rendering change comes with the
  accepted miss.
- Send `inline-tools-2026-09-15` in `anthropic-beta` on every positional request, not only
  those with tool blocks. Merge with betas the adapter already sends.
- A tool definition is at most the existing translated tool shape. No `cache_control` on it.
- Sum by-value definition bytes. Past 4,194,304 bytes raise `ConfigError` with an actionable
  message rather than send a request the API rejects with `available_tools_limit_exceeded`.

`pause_turn` maps to `end_turn` in Linch and is not resumed, so a tool block never directly
follows a paused assistant turn. Add a test that pins this.

### 6.4 OpenAI Responses rendering

Only when positional:

- Render `tool_definitions` as an item in `input` at the block's position:
  `{"type": "additional_tools", "role": "developer", "tools": [<translate_tools output>]}`.
  Position follows the log, so round-trips keep it in place. With `previous_response_id`,
  only items after the previous response are sent, which holds without extra handling.
- When any tool is inactive, or was added, and `req.tool_choice` is `None` or `"auto"`, set
  `{"type": "allowed_tools", "mode": "auto", "tools": [{"type": "function", "name": n}, ...]}`
  with the active names in stable order. Leave a forced or `"none"` choice alone.
- Do not set `prompt_cache_breakpoint` on the item. The docs say it is not accepted.

### 6.5 Required tests

Payload tests only, with a fake transport. Write each failing first.

- Option off: Anthropic and Responses payloads byte-identical to phase 2, with and without a
  block carrying definitions.
- Allowlist: `claude-sonnet-5`, unknown models, proxies, `gpt-5.3` stay append-only.
- Positional add: `tools` unchanged, one system message, follows the user text, precedes the
  next assistant turn. Earlier messages byte-identical across requests.
- Parallel tool results: the system message is after the single merged user message and never
  between `tool_use` and `tool_result`.
- Abort, then a new prompt: placement stays valid.
- Restore uses `tool_reference`. Removal uses `tool_removal`.
- Removing a by-value tool emits no `tool_removal`, and a call returns the unknown-tool error.
- Restoring it emits no second definition.
- Duplicate addition after compaction is skipped.
- Schema edit takes the phase 2 path.
- Fallback to an append-only model mid-session: added tool is in `tools`.
- Run-level `allowed_tools` narrowing takes the phase 2 path.
- Limit: over 4 MB raises `ConfigError`.
- Beta header present on every positional request, including those without a tool block.
- Responses: item position after a round-trip, `allowed_tools` for removal, forced choice kept.
- Durable resume after a native addition passes contract validation.
- MCP addition: text states the name only, definition travels only in the tool block.

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

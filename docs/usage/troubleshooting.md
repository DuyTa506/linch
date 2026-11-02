# Troubleshooting

Use this page to match setup, tool, authentication, and resume errors to their cause and fix.

[← Usage guide](./README.md)

Literal rows quote complete source messages. Fragment rows quote only fixed text
within messages that also contain dynamic details; they are not complete errors.
Source paths identify the code to search in a checkout.

## `Agent` and `ConfigError`

These checks run while constructing or normalizing an agent configuration.

| Kind | Message | Source | Cause | Fix |
|---|---|---|---|---|
| Literal | `Agent requires a model` | `src/linch/agent.py` | `model` is empty or otherwise false. | Supply a nonempty `model=`, including when using `ScriptedProvider`. |
| Literal | `max_retries must be non-negative` | `src/linch/agent.py` | `max_retries` is negative. | Set it to zero to disable retries, or use a positive value. |
| Literal | `permissions.rules must be a list` | `src/linch/agent.py` | A permissions dictionary contains a non-list `rules` value. | Pass a list of `ToolRule`, `PathRule`, or `BashRule` instances, or omit `rules`. Rule dictionaries are not accepted as entries. |
| Literal | `permissions.mode must be one of: default, acceptEdits, skip-dangerous` | `src/linch/agent.py` | The mode does not match an accepted spelling. | Choose the intended mode using its exact spelling; ordinary approval handling uses `default`. |
| Literal | `pass either limiter= or max_provider_concurrency=, not both; max_provider_concurrency is a shortcut that builds a limiter for you` | `src/linch/agent.py` | Both arguments are non-`None`. | Keep one configuration method: a supplied limiter or the concurrency shortcut. |

See [agent configuration](./agent.md) and [permission rules](./tools.md#permissions).

## `ToolRegistry`, `assert_tool_contract`, and `ToolOutput`

Registration checks the duck-typed tool shape before adding it: a non-empty name,
a valid `scope`, a dictionary `input_schema`, and callable `validate`, `execute`,
and `summarize` methods are all required. `Agent(tools=...)` expects a
`ToolRegistry`, not a plain list. For a minimal registry:

```python
from linch import Agent, empty_tools

agent = Agent(model="your-model", tools=empty_tools())
```

The separate `assert_tool_contract` helper is asynchronous, so tests must
`await` it. It raises `AssertionError` when its checks fail; its
execution-result check currently covers legacy `ToolResult` tools only.

| Kind | Message | Source | Cause | Fix |
|---|---|---|---|---|
| Literal | `tool.name must be a non-empty string` | `src/linch/tools/registry.py` | The registered tool has no nonempty string name. | Define `name` on the tool, or use `@tool` to construct its metadata. |
| Fragment | ` is missing a callable ` | `src/linch/tools/registry.py` | One of `validate`, `execute`, or `summarize` is missing or not callable; the message includes the tool and method names. | Implement the named method; all three are required. No base class is needed. |
| Literal | `tool.validate(...) must return a dict` | `src/linch/testing/contracts.py` | The contract helper received a non-dictionary validation result. | Return the normalized input dictionary from `validate`; raise for invalid input. |
| Literal | `tool.execute(...) must return ToolResult` | `src/linch/testing/contracts.py` | The legacy contract helper received a different result type. | Return `ToolResult(content="...", summary="...")` for a legacy tool. This helper check does not describe the canonical output contract below. |
| Fragment | ` declares output_schema and must not return ToolResult` | `src/linch/tools/function.py` | A function tool declares `output_schema` but returns legacy `ToolResult`; its name is included in the message. | Return a raw JSON value, `ToolOutput`, or `ToolOutputError` for canonical tool output. |
| Literal | `tool output renderer must be synchronous` | `src/linch/tools/base.py` | Rendering produced an awaitable. | Use synchronous `def render_output(value) -> str`. |
| Literal | `tool output renderer must return str` | `src/linch/tools/base.py` | Rendering produced a non-string value. | Return text from the renderer. |

Custom renderers also require nonempty stable `renderer_id` and
`renderer_version`. These rows identify the detecting stage; they do not promise
that every tool exception escapes `session.run` unchanged.
See [tools and canonical output](./tools.md#custom-tools).

## `AuthError` and provider credentials

| Kind | Message | Source | Cause | Fix |
|---|---|---|---|---|
| Fragment | `Gemini auth error:` | `src/linch/providers/gemini.py` | Gemini's adapter classified a 401/403, or a `None`/400 code whose text mentions an API key, credentials, or permission. The upstream exception follows the prefix. | Select the intended authentication mode and configure its credentials and access permissions. |

For Gemini API-key authentication, pass `GeminiProviderOptions(api_key=...)`
without `project`. Setting `project` selects Vertex AI with application default
credentials and `location`; that branch does not pass `api_key` to the client.
This classification alone does not prove that the key is invalid.

OpenAI and Anthropic map authentication-class exceptions or status 401 to
`AuthError`, forwarding upstream error text. That dynamic text is provider/SDK
dependent, so there is no fixed Linch message to quote here. Check the selected
endpoint and credentials: `OpenAIOptions(api_key=..., base_url=...)` and
`AnthropicProviderOptions(api_key=..., base_url=...)` forward those options to
their clients. See [provider configuration](./providers.md).

## `SessionBusyError` and run admission

`SessionBusyError` exposes `session_id` and the existing `run_id`. A run in
`pending`, `running`, `waiting_permission`, or `suspended` owns the session.
Queue the new prompt or report busy. After an in-process task is cancelled,
resume that run or reconcile external effects and call
`run_store.mark_failed(run_id, error={"message": ...})`; there is no crash reaper.
For disconnect handling, signal `session.abort()` and continue consuming through
the final `ResultEvent` to persist an aborted status.

| Kind | Message | Source | Cause | Fix |
|---|---|---|---|---|
| Literal | `Cannot install run admission index: duplicate nonterminal runs exist for a session; resume or mark_failed those runs before restarting.` | `src/linch/run_stores/postgres.py` | A compatible version-3 database contains overlapping nonterminal runs when the admission index is installed. | Reconcile those runs with the old deployment before upgrading; the new store cannot finish initialization until duplicates are resolved. |

## `RunContractMismatchError` and resume `ConfigError`

| Kind | Message | Source | Cause | Fix |
|---|---|---|---|---|
| Fragment | `run contract mismatch ` | `src/linch/run_store.py` | Direct `ensure_run_contract_compatible` detected incompatible stored/requested execution contracts. Fingerprints and field differences follow the prefix. | Inspect the reported differences and restore matching execution inputs and policy identity, or start a fresh run for the changed configuration. |
| Fragment | `Cannot safely resume run ` | `src/linch/loop/runner.py` | Session resume caught a contract mismatch, `TypeError`, or `ValueError` and wrapped it in `ConfigError`. The run ID and underlying error follow the prefix. | Read the underlying error; when it contains the mismatch fragment above, apply that row's fix. |

A run contract includes models, system blocks, offered tool schemas/order,
input, output settings, run options, budget, and policies. Changing only a stored
fingerprint cannot repair incompatibility: the comparison also recomputes it
from the payload. The direct helper raises `RunContractMismatchError`; the session
resume path wraps it in `ConfigError`. Missing or old persisted wire versions
are a separate failure family. Package-version-only and top-level
tool-description-only edits do not invalidate an otherwise matching contract.
For 3.1.0, drain all active runs before deploying: Local/Docker policy versions
are now `2`, and Bash schemas changed. Those changes still reject older
contracts despite the unchanged wire generation. See
[production upgrade guidance](./production.md#4-resume-after-a-restart) and
[durable run stores](./run-stores.md).

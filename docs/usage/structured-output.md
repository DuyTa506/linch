# Structured output

Use `OutputSchema` when an agent must return a final answer matching a JSON Schema.

[← Usage guide](./README.md)

When you need the agent's final answer as a typed object rather than free text,
attach an `OutputSchema`. Providers that support structured output can be asked
to constrain their response to your JSON Schema. The parsed object surfaces on
the result event as `structured_output`. This page covers the schema, the two capture paths
(JSON-text parsing vs. native final-tool capture), and the closed-loop
schema-repair retry.

---

## OutputSchema

Define the shape you want with `OutputSchema` and pass it as
`Agent(output_schema=...)` (or per run via `RunOptions(output_schema=...)`):

```python
from linch import Agent, OutputSchema

schema = OutputSchema(
    name="invoice",
    schema={
        "type": "object",
        "properties": {
            "total": {"type": "number"},
            "line_items": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["total", "line_items"],
        "additionalProperties": False,
    },
    strict=True,
)

agent = Agent(model="gpt-5", output_schema=schema)
# result.structured_output → {"total": 42.50, "line_items": [...]}
```

The fields are:

| Field | Meaning |
|---|---|
| `name` | Schema identifier sent to the provider (required by most APIs). |
| `schema` | A valid JSON Schema `dict` describing the output shape. |
| `strict` | Enable strict schema validation at the provider level (supported by OpenAI). Defaults to `True`. |
| `description` | Optional human-readable description of the schema. |

The parsed object is delivered on the terminal `ResultEvent` as
`structured_output`; on failure that field is `None` and `structured_error`
carries the parse/validation message. A structured-output error alone does not
force an error result subtype: inspect both fields even when `subtype` is
`"success"`. Hooks can change the final outcome. See [Events](./events.md) for
the full result shape.

---

## Capture paths

Linch captures structured output from final text or a terminal tool. Provider
capability and explicit configuration determine the path.

### JSON-text parsing (default)

For providers without native structured output, the loop reads the model's
final assistant text, parses it as a JSON object, and validates it against the
configured schema. If raw text is not JSON, the parser also tries a fenced JSON
block. Provider-side schema constraints depend on the provider; instruct the
model to return a JSON object when using a provider without them.

### Native final-tool capture

When a provider declares `structured_output_terminal_tool` in its
`ProviderCapabilities` (the explicit Anthropic-compatible fallback), the loop
wires your schema's `name` as a **terminal tool**. The model "calls" that tool
with arguments matching the schema, and the loop captures `final_block.input` as
`structured_output` without executing the tool. Linch validates the captured
input against the configured schema before returning it. A terminal tool call
must be the only tool call in its assistant message. Native Claude,
OpenAI, Gemini, and DeepSeek instead return JSON text in their own response
formats and use the normal parse-and-validate final-answer path.

You can also force this path explicitly with `final_tool_name`
(`Agent(final_tool_name=...)` or `RunOptions(final_tool_name=...)`); an explicit
value wins over the capability-driven auto-wiring. The resolution order is:
`RunOptions.final_tool_name` → `Agent.final_tool_name` → schema name when the
provider explicitly declares terminal-tool structured output.

### OpenAI-compatible endpoints: `json_mode` (DeepSeek and friends)

`OpenAIChatCompletionsProvider` drives both real OpenAI and OpenAI-compatible
endpoints (DeepSeek, etc.). It has two `response_format` modes, selected by
`json_mode` on `OpenAIChatProviderOptions`:

| `json_mode` | request `response_format` |
|---|---|
| `False` *(default)* | `{"type": "json_schema", "json_schema": {...}}` |
| `True` | `{"type": "json_object"}` |

Some OpenAI-compatible endpoints support JSON-object mode but not schema mode.
JSON-object mode does not enforce your schema at the API, so pass
`json_mode=True` when the endpoint requires it:

```python
from linch import (
    OpenAIChatCompletionsProvider,
    OpenAIChatProviderOptions,
)

provider = OpenAIChatCompletionsProvider(
    OpenAIChatProviderOptions(
        api_key=..., base_url="https://api.deepseek.com", json_mode=True
    )
)
```

With `json_object`, Linch **text-parses and validates** the response against
your `OutputSchema.schema` (the JSON-text path above),
so you still get a checked `structured_output`. Native `AnthropicProvider` uses
`output_config.format` JSON schema. Its explicit `api_mode="compatible"` fallback
uses a generated terminal tool only for endpoints that lack that native format.

---

## Schema validation

`jsonschema` is a core dependency. Linch always checks parsed JSON against the
configured `OutputSchema.schema` before returning successful structured output;
JSON parsing and the object-root check still run first. Validation failures
populate `structured_error` and can trigger the configured repair retries.

---

## Closed-loop schema repair

By default, a final answer that fails to parse or validate terminates the run
with `structured_error` set. To instead let the agent fix its own output, enable
the schema-repair retry:

```python
agent = Agent(model="gpt-5", output_schema=schema, structured_output_retries=2)
```

When the final answer fails parsing or schema validation and retries remain,
the loop injects the error as a system-reminder user message and runs
another turn — instructing the model to respond again with only a JSON object
matching the schema. Each gate action emits a `VerificationEvent`
(`verifier="output_schema"`, `action="retry"` / `"exhausted"`). On the
final-tool path, the loop first acknowledges the captured tool call, then adds
the repair feedback as a user message.

Important behavior to keep in mind:

- With the default of `0`, schema repair adds no retry turns or verification
  events; the error remains on the result unless a hook changes the outcome.
- Repair retries count toward `max_turns` and the run budget, so a strict schema
  cannot loop unboundedly. See run budgets in [Agent & session](./agent.md).
- When retries are exhausted, the result retains `structured_error` and
  `structured_output` is `None`; the loop emits an `action="exhausted"`
  `VerificationEvent`, unless a hook changes the outcome.
- Retry counters are per-run and not checkpointed — a resumed run starts fresh.

The schema-repair gate is the built-in member of a broader family of
closed-loop verification gates (custom `Verifier`s, `ScorerVerifier`, and
`stop_when` predicates). For those, and for the `BeforeFinalAnswer` chokepoint
that drives final-answer verification, see [Hooks](./hooks.md).

---

## Related pages

- [Events](./events.md) — `ResultEvent.structured_output` and `VerificationEvent`.
- [Hooks](./hooks.md) — verifier gates and the `BeforeFinalAnswer` chokepoint.
- [Agent & session](./agent.md) — run budgets that cap repair retries.
- [Providers](./providers.md) — which providers support native structured output.

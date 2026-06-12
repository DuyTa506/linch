# Structured output

[← Usage guide](./README.md)

When you need the agent's final answer as a typed object rather than free text,
attach an `OutputSchema`. The provider is asked to constrain its response to your
JSON Schema, and the parsed object surfaces on the result event as
`structured_output`. This page covers the schema, the two capture paths
(JSON-text parsing vs. native final-tool capture), and the closed-loop
schema-repair retry.

---

## OutputSchema

Define the shape you want with `OutputSchema` and pass it as
`Agent(output_schema=...)` (or per run via `RunOptions(output_schema=...)`):

```python
from linch.types import OutputSchema

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

agent = Agent(..., output_schema=schema)
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
carries the parse/validation message. See [Events](./events.md) for the full
result shape.

---

## Capture paths

Linch captures structured output in one of two ways, chosen automatically based
on provider capability.

### JSON-text parsing (default)

For providers without native structured output, the loop reads the model's
final assistant text and parses it as JSON, then optionally validates it against
your schema. This is the fallback path and works with any provider — the model
is simply asked to emit a JSON object as its final answer.

### Native final-tool capture

When the provider declares `structured_output` in its `ProviderCapabilities`
(for example `AnthropicProvider`, which uses a forced-tool method), the loop
wires your schema's `name` as a **terminal tool**. The model "calls" that tool
with arguments matching the schema, and the loop captures `final_block.input` as
`structured_output` directly — without executing a real tool. This is more
reliable than parsing JSON out of prose because the provider constrains the tool
arguments to your schema.

You can also force this path explicitly with `final_tool_name`
(`Agent(final_tool_name=...)` or `RunOptions(final_tool_name=...)`); an explicit
value wins over the capability-driven auto-wiring. The resolution order is:
`RunOptions.final_tool_name` → `Agent.final_tool_name` → schema name when the
provider supports native structured output.

---

## Schema validation

Validation uses the optional `jsonschema` library. **When `jsonschema` is not
installed, schema validation is skipped** — the parsed JSON object is returned
as-is and `structured_error` stays `None`. Install it (`pip install jsonschema`)
if you want the parsed output checked against `schema` before it is handed back.
Without it you still get JSON parsing and type-of-root checks (the result must be
a JSON object), just not full schema validation.

---

## Closed-loop schema repair

By default, a final answer that fails to parse or validate terminates the run
with `structured_error` set. To instead let the agent fix its own output, enable
the schema-repair retry:

```python
agent = Agent(..., output_schema=schema, structured_output_retries=2)
```

When the final answer fails schema validation and retries remain, the loop
injects the validation error back as a system-reminder user message and runs
another turn — instructing the model to respond again with only a JSON object
matching the schema. Each gate action emits a `VerificationEvent`
(`verifier="output_schema"`, `action="retry"` / `"exhausted"`). On the native
final-tool path the bounce-back is handled specially: the unanswered terminal
`tool_use` is answered with an error `tool_result` carrying the repair feedback,
keeping the message history valid for the next provider call.

Important behavior to keep in mind:

- **Default `0` is byte-identical to the legacy single-attempt path** — no extra
  turns, no events.
- Repair retries count toward `max_turns` and the run budget, so a strict schema
  cannot loop unboundedly. See run budgets in [Agent & session](./agent.md).
- When retries are exhausted, the answer is **accepted as-is** with an
  `action="exhausted"` `VerificationEvent`, not failed.
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

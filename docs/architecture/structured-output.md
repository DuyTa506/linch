# Structured Output Paths

Use this page to understand how final text and terminal tools produce structured results.

> Part of the [Linch architecture guide](./README.md).

Two independent mechanisms surface the same field: `ResultEvent.structured_output: dict | None`.

```mermaid
flowchart LR
    subgraph A["Path A — Text Parse\noutput_schema on Agent or RunOptions"]
        direction TB
        A1["Provider emits JSON text\nvia response_format or text.format"]
        A2["_parse_structured_output(final_text, schema)\nrequired jsonschema validation"]
        A3["ResultEvent.structured_output = parsed\nResultEvent.structured_error = msg | None"]
        A1 --> A2 --> A3
    end

    subgraph B["Path B — Forced Tool\nfinal_tool_name on Agent or RunOptions"]
        direction TB
        B1["Model calls final_tool_name\nstop_reason = tool_use"]
        B2["loop/ intercepts ToolUseBlock\nbefore scheduler — tool is NOT executed"]
        B3["Validate input against configured schema\nResultEvent.structured_output = input | None"]
        B1 --> B2 --> B3
    end
```

`jsonschema` is a core dependency; configured schemas are always validated.

## Design rationale

- **Two capture paths.** The final-text path parses a JSON object and validates it
  against the configured schema. Providers that support structured output can
  constrain their responses; other providers may require prompting for JSON.
- **Terminal tool capture.** An explicit `final_tool_name`, or a provider's
  `structured_output_terminal_tool` capability with a schema, selects the terminal
  tool path. Its input is validated against the schema when one is configured.
  A validation error sets `structured_error` and leaves `structured_output` empty.
- **The final tool is intercepted.** The loop captures the named tool call before
  scheduling rather than executing it. It pairs tool calls with results in the
  session history.
- **Repair retries.** With `structured_output_retries` greater than zero, parse or
  validation errors can prompt another turn. When retries are exhausted, the
  result retains the structured error unless a hook changes the outcome. A
  structured error alone does not force an error result subtype: callers must
  inspect `structured_error` and `structured_output` even on success. Retries
  are subject to run limits.

---

Back to the [architecture index](./README.md).

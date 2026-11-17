# DeepSeek live e2e: expectations (written before any measurement)

Build under test: `bin/linch-server-debug` at f69d97b2 (stack tip, PR #167), Python client `e2e/pysrc`.
Provider `deepseek`, model `deepseek-flash`, thinking disabled unless a row says otherwise. Every provider call goes
through a local counting gateway on 127.0.0.1 (random port) that forwards to https://api.deepseek.com, logs header
NAMES only, request shape (message hashes, tool_choice, max_tokens) and the raw `usage` JSON. Budget: 30 requests total
forwarded, `max_tokens` <= 300 (<= 600 for the compaction run).

Sources: docs/usage/providers.md (request settings, concurrency, cost), docs/usage/monitoring.md (pricing),
docs/usage/structured-output.md, docs/architecture/compaction.md (summary call), docs/architecture/providers.md (DeepSeek),
docs/usage/agent.md (budget).

| # | Feature | Expected observable | Threshold / count | Why |
|---|---|---|---|---|
| 1a | final-tool structured output | run `success`, `structured_output` equals schema-valid object; `init.final_tool == "person"`; request carries no `response_format`/schema, one tool named `person` | provider calls (`usage` events) = 1, assistant events calling `person` (final_calls) = 1, no `tool_call_start` for `person` | providers.md: DeepSeek `output_schema: final_tool`; turn-lifecycle: final tool forced when it is the only tool and thinking is Disabled |
| 1b | forced choice on the wire | the request of 1a carries a forced `tool_choice` (`required` or the named function) and DeepSeek answers 200 | status 200 | architecture/providers.md: forced choice allowed unless thinking is not Disabled |
| 1c | `tool_choice: required`, named choice, 2 host tools | both accepted (HTTP 200), first assistant message has a tool_use; named one calls exactly that tool | 1 request each (`max_turns` 1) | structured-output.md `tool_choice` |
| 1d | forced choice with thinking on | `provider_capabilities` says forced refused (no request); a run with forced choice + thinking enabled is refused locally with no HTTP request. Raw API probe (1 request): what DeepSeek itself answers (expected 400 mentioning tool_choice / thinking) | 0 forwarded for the run; 1 for the probe | architecture/providers.md: refused locally as InvalidRequest |
| 2 | prompt cache, 2 requests with a ~2000-token identical system prefix | request 1: hit ~0 (cold) (may be warm from earlier use); request 2 (sent >= 5 s later): `prompt_cache_hit_tokens` > 0; hit ratio recorded | hit > 0 on req 2; expected ratio >= 0.8 of prompt tokens | DeepSeek context caching is automatic by prefix |
| 2b | cost arithmetic | `usage.cost_usd` of each call == (miss*0.3 + hit*0.006 + completion*1.2)/1e6 using the gateway's raw usage; run `total_cost_usd` == sum | abs diff <= 1e-9 | monitoring.md: `input` is the uncached input only, `cache_read` the hit rate; pricing per agent |
| 3a | compaction summary shares the primary prefix | A session seeded (0-cost scripted provider, Postgres) with ~3000 estimated tokens; turn A on DeepSeek (primary, prefix gets cached), turn B makes the view due; the summary request is the request B's primary call would send plus ONE trailing user message: the gateway shows the message-hash list of the summary request starting with the full list of request A (common prefix = all of A's messages) and exactly one extra trailing message after the logged view | shared prefix == len(A.messages) messages; summary request messages = A + [assistant A, user B] + 1 instruction | architecture/compaction.md "The summary call" |
| 3b | cache reuse on the summary request | raw `usage.prompt_cache_hit_tokens / prompt_tokens` of the summary request | > 0.50 (expected ~0.7-0.85) | claim: cached prefix from the last primary call serves the summary call too |
| 3c | checkpoint | `compaction` event: strategy `summary`, not rejected, `tokens_after < tokens_before`; checkpoint carries `[user message, verbatim]` blocks (`keep_user_tokens` default 15000); the primary call of turn B answers the code word given in a folded user message | tokens_after < tokens_before; verbatim blocks >= 1; answer contains the code word | compaction.md "The checkpoint" |
| 3d | next turn continues | a further turn after the compaction (reusing the compacted view) succeeds | result success | |
| 4 | #164 rejection hold | NOT RUN live (needs a model that refuses to summarize); deterministic tests cover it | | |
| 5a | cost cap, no pricing | `open_agent` with `budget.max_cost_usd` and no pricing for deepseek-flash fails with -32011 naming the model; 0 requests | error code -32011 | agent.md `budget` |
| 5b | cost cap with pricing | the run of 2 succeeds under a generous cap with pricing; a cap of 1e-6 USD trips: `budget` event `exceeded`, `error` BudgetExceededError, `result` subtype error | the tiny-cap run makes exactly 1 request (the next call is refused) | agent.md `budget` |
| 6 | effort / thinking | `effort: high` + thinking enabled accepted (HTTP 200); request carries `reasoning_effort` high and thinking enabled; raw usage reports `completion_tokens_details.reasoning_tokens` > 0 | 200; reasoning_tokens > 0 | architecture/providers.md DeepSeek: effort low/high/max, medium and xhigh map to high |
| 7 | `max_provider_concurrency` | 3 concurrent tiny runs. Gate 1: gateway request intervals do not overlap (each start >= the previous end). Gate 3: at least two intervals overlap | max in-flight at the gateway: gate 1 = 1, gate 3 >= 2 | providers.md "Limit concurrent calls" |
| 8a | `request.default_headers` | the custom header name `x-e2e-probe` reaches the gateway on every request of an agent that sets it; the Authorization header is still the key from the environment (name seen, value never logged) | header seen on 100% of requests | providers.md request settings |
| 8b | scrub | the gateway answers 400 with a body that contains the custom header's value (>= 8 bytes, harmless name); the run's `error` text does not contain the value (shows `[redacted]`) | value absent from every event of the run | providers.md: removed from error text when >= 8 bytes |

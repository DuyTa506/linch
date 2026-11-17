# DeepSeek live e2e results (2026-11-18)

Build: `bin/linch-server-debug` at f69d97b2; Python client `e2e/pysrc`; model `deepseek-flash` (the API reports
`deepseek-flash` as the serving model on every call); thinking off unless stated; local mode `Server()` with the
provider `base_url` pointing at a counting gateway (`src/gateway.py`) that forwards to api.deepseek.com.
Raw data: `live/*.json` (per scenario, events + gateway records with raw usage JSON), `live/gateway.jsonl`,
`live/bodies/NNN.json` (every forwarded request body). Scripts: `src/`.

DeepSeek requests forwarded: 28 of 30 (27 x HTTP 200, 1 x HTTP 400 = the raw probe of a forced tool_choice with
thinking on). Balance before 1.82 USD, after 1.82 USD (the guard prints two decimals; estimated spend about 0.015 USD (about 3 x 0.004 for the compaction runs, the rest under 0.003):
the balance endpoint did not move at two decimals).
Load average (1 min) at the runs: 2.2 to 3.2 (`uptime` recorded inside the JSON as `load`).
Mock rehearsal of every scenario ran first through the gateway's canned replies (no network, `rehearsal/`).

## Table

| feature | expected | observed | verdict | evidence |
|---|---|---|---|---|
| 1a final tool | success, schema-valid, 1 provider call, final_calls=1, `init.final_tool=person` | success, `{"name":"Ada Lovelace","age":36}`, usage events 1, assistant calls of `person` 1, no tool_call_start for `person`, request had 1 tool (`person`), no `response_format` | MATCH | live/s1.json `final_tool`, gateway seq 5 |
| 1b forced choice on the wire | forced tool_choice, API 200 | `tool_choice={"type":"function","function":{"name":"person"}}`, thinking `disabled`, HTTP 200, finish_reason `tool_calls` | MATCH | gateway seq 5 |
| 1c required / named | accepted, tool_use in first reply | required: HTTP 200, model called `clock`; named `lookup`: HTTP 200, called `lookup`; both end `max_turns` (1 call each) | MATCH | gateway seq 6, 7 |
| 1d forced choice + thinking on | refused locally, no request; raw API answer | `open_agent` OpenError -32011 "DeepSeek rejects a forced tool_choice while thinking is on; use tool_choice auto, or disable thinking"; `provider_capabilities.tool_choice.forced_refused_with = [unset, adaptive, budgeted]`. Raw API probe (thinking enabled + `tool_choice: "required"`): HTTP 400 `Thinking mode does not support this tool_choice` | MATCH | s1.json `thinking_forced_open`, `probe` |
| 2 cache, two requests | req 2 hit > 0 | req 1 hit 0 / prompt 2854; req 2 (6 s later) hit 2688 / prompt 2854 = 0.9418 (repeat: identical 2688/2854) | MATCH | s2.json, s2_r2.json |
| 2b cost arithmetic | linch cost = (miss*0.3 + hit*0.006 + out*1.2)/1e6 to 1e-9 | call 1: 0.0008574 vs 0.0008573999999999999; call 2: 6.7128e-05 exact (repeat 6.8328e-05); max diff 1.1e-19; run total_cost_usd = per-call cost | MATCH | `src/cost_check.py live s2`, `s2_r2` |
| 3a summary shares the primary prefix | summary request = A + one trailing instruction | A has 8 messages (system + 7); summary request has 11 = the 8 identical + assistant A + user B + 1 trailing user instruction; common prefix = 8 of 8 messages in 3 of 3 runs. (The one trailing message is the instruction; the 2 before it are the new logged messages.) | MATCH | `analysis` in s3_r1..r3 |
| 3b cache hit on the summary request | > 0.50 | raw usage: prompt 5055, `prompt_cache_hit_tokens` 4224 = 0.8356 (run 1), 0.8356 (run 2), 4224/5056 = 0.8354 (run 3); previous primary request 4.05 s earlier | MATCH | s3_r*.json requests[1].usage |
| 3c checkpoint | summary accepted, tokens_after < tokens_before, verbatim section, answer continues | `compaction` event: strategy summary, not rejected, tokens 3426 -> 2060 / 2231 / 2012, 3 `[user message, verbatim]` blocks, summary header `<summary of earlier conversation>`; model summary quotes the code word. Primary B answer: run 2 and 3 `MARMOT-7741`; run 1 "I can't share that." because my prompt called it a "secret code word" and the model took it as a confidentiality rule (model behaviour; wording changed for runs 2-3) | MATCH (run 1 answer: model behaviour) | s3_r*.json, `src/checkpoint.py` on bodies |
| 3d next turn | success | turn C success in all runs; its request hit 0.9405 / 0.9303 / 0.9515 of its prompt (cache of the compacted prefix, 2688-2816 of ~2900 tokens) | MATCH | s3_r*.json requests[3] |
| 4 rejection hold (#164) | not testable live | NOT RUN: needs a model that refuses to summarize; deterministic tests cover it | NOT RUN | |
| 5a cost cap w/o pricing | open refused -32011 naming the model | `invalid agent: budget.max_cost_usd needs a price for the model "deepseek-flash", which has none: set pricing for it or cap budget.max_tokens instead (code -32011)`; 0 requests | MATCH | s5.json `no_pricing_open` |
| 5b cost cap with pricing | generous cap runs (done in s2: `budget.max_cost_usd` 0.05 + pricing, success); 1e-6 cap trips after 1 request | tiny cap: 1 request (tool call), usage cost 9.1968e-05, `budget` warning then `exceeded`, error `BudgetExceededError`, result subtype `error`, no second call | MATCH | s5.json `tiny_cap`, gateway seq 11 |
| 6 effort high + thinking | 200; `reasoning_effort=high`, thinking enabled; reasoning tokens in usage | request: `thinking={"type":"enabled"}`, `reasoning_effort="high"`, HTTP 200; raw usage `completion_tokens_details.reasoning_tokens=13` of completion 15; linch's usage event has 4 buckets only (`output_tokens=15`, reasoning folded into output, no separate field; docs promise none); cost 3.48e-05 | MATCH | s6.json |
| 7 `max_provider_concurrency` | gate 1 serial, gate 3 overlap | gate 1: gateway intervals 0.000-1.041, 1.047-1.602, 1.608-2.321 s (peak in flight 1, wall 2.33 s); gate 3: all three start at 0.000 s (peak 3, wall 1.16 s). Fresh server per gate | MATCH | s7.json |
| 8a default_headers | custom header on every request | `x-e2e-probe` present on 27/27 forwarded requests of agents that set it (the raw probe, which did not set it, is the only one without); `authorization` name present on all (value not logged) | MATCH | gateway.jsonl, `src/summary.py` |
| 8b scrub | value absent from run's error text | gateway 400 body quoted the header value (18 bytes, name harmless): error event message `provider returned HTTP 400: invalid_request_error: Invalid request: header x-e2e-probe rejected with value [redacted]`; value absent from every event of the run | MATCH | s8.json |

## Measurements

- Prompt cache: DeepSeek cache blocks are 64 tokens (all hit counts are multiples of 64: 4224 = 66 x 64, 2688 = 42 x 64).
- Compaction summary request: hit ratio 0.8356 / 0.8356 / 0.8354 (n = 3 runs, same seeded session shape, nonce differs per run so no run is warmed by another); expected cache hit = the primary prefix up to the last message of request A (4342 prompt tokens, 4224 cached = block floor).
- Compacted view then used on the next turn: prompt 2807-3009 tokens vs 4342 before compaction (summary request itself 5055); turn C hit ratio 0.93-0.95.
- Summary call: completion 149-352 tokens (limit 500), duration 1.5-3.0 s (`compaction.duration_ms`).
- Request-count limit: the 3-repeat rule was met for compaction (3) and caching/cost (2); the other rows ran once (30-request cap).

## Defects

None found. No RED test written. Notes (not defects): (1) the linch usage event has no reasoning-token field (the
four buckets only; reasoning is inside `output_tokens`); (2) the token estimator (chars / 4, text only) says 3426
tokens for a view the API counts as ~4300 (pseudo-random hyphenated words tokenise at ~2.8 chars per token), so
`threshold_tokens` is in estimator units, not API tokens.

## Not run / limits

- #164 rejection hold: not testable live.
- Everything is on the debug binary (correctness); the concurrency timings (s7) are gateway-side, not a latency
  benchmark.
- Hit ratio repeats: compaction n = 3, cache n = 2; one sample of each other row.

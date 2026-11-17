# core area: expectations (written before any run; stack tip f69d97b2, PRs #145 #146 #148 #150 #153 #163 #164 #167)

Provider: vLLM `qwen3.6-35b-a3b` through the proxy 127.0.0.1:18201 only. Server: `linch-server` over stdio
(Python `Server()`), Postgres `par_3`, one fresh namespace per test. Debug binary for correctness, release
binary for the measurements. Sources: docs/usage/{agent,structured-output,providers,events,tools,subagents,server}.md
and the PR bodies. "Run" = one `agent.run`. Counts are of provider calls seen in `e2e/vllm-proxy.log`
or of events in the stream.

| # | Feature | Exact observable / threshold | Why (source) |
|---|---|---|---|
| 1a | Basic run | Stream has `init` first, at least one `assistant`, `usage` per call, `result` last with subtype `success`, `final_text` non-empty; `init.provider == "vllm"`, `init.model == model`. Row in Postgres `linch_runs` is terminal-success. | events.md, runs.md |
| 1b | Usage | `result.total_usage` has four buckets, `usage_complete` true; `output_tokens` equals the sum of `usage.output_tokens` of the usage events and the proxy `completion_tokens` of the same request; without `pricing` `total_cost_usd is None` and each `usage.cost_usd is None (built-in table is Anthropic only). | events.md Usage and cost |
| 1c | Cost with pricing | With `pricing={model:{input:p_in, output:p_out}}` (USD per million): `total_cost_usd == (input*p_in + output*p_out + cache_read*p_in + cache_creation*p_in)/1e6` within 1e-9 (cache_read defaults to `input`); the last `cumulative_cost_usd` equals the total. | config.py ModelRates, PR #163 |
| 2a | Tool loop | Host `read` tool called; result read by the model; `tool_call_start` precedes `tool_call_end`; answer contains the tool's value; the run ends on the first text-only response: `assistant` events == tool turns + 1. | CLAUDE.md loop rule, tools.md |
| 2b | Built-in read tools | With `workspace=tmp`, `bash=False`: Read/Glob/Grep calls succeed (is_error False) and the answer contains a value found only in the files. | tools.md Workspace tools |
| 2c | Parallel reads, serial writes | Two read host tools in one response sleeping 1 s each run with overlapping intervals (max(start) < min(end)), batch wall < 1.8 s. Two `write` host tools (skip_dangerous) in one response run serialised: intervals do not overlap. | tools.md Scopes |
| 3a | Capabilities output_schema | `provider_capabilities(vllm).output_schema == "final_tool"`. | structured-output.md |
| 3b | Final tool run | With `output_schema`: `init.final_tool == schema name` and init.tools lists it; no `tool_call_start/end` for it; `result.subtype == success`, `structured_output` validates against the schema (jsonschema), `final_text` is its JSON text. | structured-output.md, PR #146 |
| 3c | Unsatisfiable schema | Schema no value satisfies (`integer` with minimum 10 and maximum 5), `structured_output_retries=2`: run ends `failed`, `error.name == StructuredOutputError`; `verification` events: 2 `retry` (attempt 1, 2) then 1 `stop`; provider calls == 3 (initial + 2 retries) counted from `usage` events and the proxy log; `result.structured_error` set. | structured-output.md |
| 4a | tool_choice required | `{"type":"required"}` with `max_turns`: first assistant message has a tool call although the prompt asks for plain text. | PR #146 |
| 4b | tool_choice named | `{"type":"tool","name":"lookup"}`: the first response calls `lookup`. | PR #146 |
| 4c | tool_choice none | Tools offered, prompt asks to use one: first assistant message has no tool call. | PR #146 |
| 4d | Named tool not offered this turn | A `before_provider_call` hook removes the named tool from the request: the request goes out as `auto` (the run does not fail with a provider 400); `context_build.metadata.tool_choice_dropped == <name>`. | structured-output.md, PR #148/#164 |
| 4e | Forced choice needs max_turns | On a tenant server (`--config`, admission), `agent.open` with `tool_choice required` and neither `max_turns` nor `final_tool_name` is refused (-32004, kind `tool_choice`, message "a forced tool_choice (required, or a tool that is not the final tool) needs spec.max_turns or spec.final_tool_name, or the run could never end"); with `max_turns` it opens. On trusted stdio no admission applies. | admit.rs, server.md |
| 5a | Effort accepted | `effort` low, medium, high, xhigh, max all run to a `result` without an error. | providers.md |
| 5b | Effort mapping | `high` and `max` are sent as `xhigh` (vLLM Qwen3 template takes only low/medium/xhigh): no 400, and completion tokens of high/max are not different from xhigh beyond noise (the wire value is not visible to a client hook, so the proof is statistical). | providers.md l.237-244 |
| 5c | Reasoning rises with effort | Mean completion tokens (3 repeats per level, same prompt, max_output_tokens 600) satisfy mean(low) < mean(medium) <= mean(xhigh). Caveat: at the 600 cap high levels may saturate at the cap; equality at the cap is the limit, not a failure. | providers.md |
| 5d | thinking off | `thinking: disabled`: no thinking deltas in the stream and completion tokens below 0.5x the low-effort mean. | existing basics suite |
| 6a | temperature 0 | 3 runs of a short prompt at temperature 0: identical text in at least 2 of 3. | providers.md Temperature |
| 6b | temperature bounds | 2.5, -0.1 and NaN fail `agent.open` (OpenError -32011 or protocol error); 2.0 and 0 open. | agent.md `temperature` |
| 7a | Cost cap unpriced | `budget.max_cost_usd` on vLLM with no pricing: `agent.open` refused, code -32011, message names `qwen3.6-35b-a3b` and says to set `pricing` or cap `max_tokens`. | PR #163 |
| 7b | Cost cap priced trips | With pricing and `max_cost_usd=1e-9`: `budget` event kind `exceeded`, `error.name == BudgetExceededError`, result subtype `error`, not success. The cap trips before the NEXT call, so a 2-turn tool prompt makes exactly 1 provider call (proxy log + usage events). | agent.md budget |
| 7c | max_tokens cap | `budget.max_tokens=50` with a tool prompt: `budget exceeded` before the second call, `BudgetExceededError`, 1 provider call. | agent.md budget |
| 7d | call_timeout_ms bounds | 0 and 99 refused at open (-32011 naming the field); 100 accepted. | PR #163 |
| 8a | Loop guard identical | Same read tool, same input, same result: `loop_guard` event reason `repeated_tool_call`, tool body ran exactly 3 times, run `failed` with `LoopGuardError`, before `max_turns` (12). | agent.md loop_guard, PR #145 |
| 8b | Fix-and-test not tripped | 10 alternations of a write tool `apply_patch(n)` (new input each time) and an exec-like host tool `run_tests()` (same input, same result): no `loop_guard`; all 10+10 calls run. Needs the model to follow a 20-call script; an early stop is model behaviour, rerun once. | PR #164 |
| 8c | polls host tool | `polls=True` host tool `job_status` answering the same text forever, `loop_guard.max_idle_turns=4`: run ends `failed` with a `loop_guard` event after more than 4 identical answers (count > limit), before `max_turns`; control without `polls`: trips at the 3rd identical call. | loop_guard.rs, PR #148 |
| 9a | Prerequisite | `policy.prerequisites [{tool: deploy, requires: [build]}]`: a model told to call deploy first gets an error result naming `build` and a `policy` event `rule=prerequisite action=refuse tool=deploy missing=[build]`; deploy body not run until build succeeded in an earlier batch; afterwards it runs. | agent.md policy, PR #153 |
| 9b | Completion gate | gate `{name: tested, succeeded: [run_tests]}`: a model that answers without calling `run_tests` gets `policy rule=completion action=correct attempt=1`; with `max_corrections=1` and a prompt that forbids calling the tool, the second failure ends `failed` with `PolicyGateError` and `policy action=stop`. Corrections == `max_corrections`. | agent.md Gates |
| 9c | Policy + forced choice | `policy` with a prerequisite plus `tool_choice none` (and a named tool with a prerequisite) is refused at `agent.open` (-32011). | agent.md policy |
| 10 | Provider limiter | `max_provider_concurrency: 1`, 2 concurrent runs: the two requests' proxy service windows (ts - service_s .. ts) never overlap. The proxy serialises anyway, so non-overlap alone proves nothing. Agent-side proof: with the cap, the second run's request reaches the proxy only after the first's response ended (proxy `wait_s` ~ 0 for both; request arrival = ts - latency_s - wait_s, so arrival of request 2 >= ts of request 1); without the cap both requests arrive within ms of each other and request 2 has `wait_s` > 0. | PR #148 |
| 11a | pre_tool_use deny | `hook` event point `pre_tool_use` outcome block; tool body never ran; `tool_call_end.is_error` carrying the reason. | tools/hooks docs |
| 11b | pre_tool_use rewrite | `tool_call_start.input` is the rewritten input; the tool body saw it. | |
| 11c | post_tool_use | replaced result text in `tool_call_end`; `hook` event present. | |
| 12a | Skills | Model calls `Skill`; `skill_invoked` and `skill_completed` events; the answer carries the skill token. | skills.md |
| 12b | Subagent | `Subagent` tool (the doc name; there is no `Task` subagent tool, `Task*` are session task tools): `SubagentEvent` wrapped child events; child `init.tools == [Read]`; parent answer carries the child's finding. | subagents.md |
| 12c | Parallel read subagents | Two `Subagent` calls in one response, type listing only read tools: child lifetimes overlap (second child's init before first child's result) and parent wall < sum of the children's walls. Caveat: the proxy serialises model calls, so overlap is of lifetimes, not of generation. | skills-subagents.md |
| 13a | Capabilities | `context_window == 131072`, `context_source == "server"`, `output_schema == "final_tool"`, tool_choice required and named true. | providers.md |
| 13b | Models | `list_models` contains `qwen3.6-35b-a3b`; `model_info` of it works; `model_info("no-such-model")` raises `ModelNotFound` (-32036). | providers.md |

## Measurements (release binary; load average recorded next to each number)
| M | What | Expectation |
|---|---|---|
| M1 | linch overhead per turn = median wall(1-turn text run, agent open excluded) - median wall(same request direct to the proxy, streaming, same `max_tokens`), 5 repeats each, temperature 0 | tens of ms or less (SSE parsing and ~7 group-committed Postgres writes per turn); expect < 100 ms. Model output length noise dominates, so completion_tokens and the proxy `latency_s` are compared too. |
| M2 | `agent.open` p50 over 10 | < 250 ms on an already migrated schema. |
| M3 | events/s through the client for a 200-event run | > 2000 events/s with a scripted provider (no vLLM load); sample reduced to avoid vLLM load, said in the report. |
| M4 | RSS of the server idle vs after 20 runs | idle < 100 MB; growth after 20 runs < 30 MB. |

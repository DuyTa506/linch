# Area core: results (live run 2026-11-18, stack tip f69d97b2, vLLM qwen3.6-35b-a3b through the proxy)

Source: `e2e/results/core/` (`expectations.md`, `*.events.jsonl`, `*.wire.jsonl`, `static.jsonl`, `5-effort.jsonl`,
`measure-m*.jsonl`, `attempts.jsonl`). Correctness checks ran on the debug binary, 1 run each unless a row says otherwise
(`attempts.jsonl`: every check passed on attempt 1). Measurements M1-M4 ran on the release binary, 64-core shared host,
load average 2.4-3.2 (recorded per file). The file holds no verdicts; every verdict below is derived by comparing
`expectations.md` with the data.

## Checks

| id | feature | expected | observed | verdict |
|---|---|---|---|---|
| 1a/1b | basic run, usage without pricing | init first, result success, usage buckets equal wire tokens | types init,user,context_build,assistant,usage,result; total_usage 36 in / 2 out = proxy prompt 36 / completion 2 | MATCH |
| 1c | cost with pricing | reported cost equals formula within 1e-9 | 2 runs: expected 1.714e-05 vs reported 1.714e-05; 1.985e-05 vs 1.985e-05 | MATCH |
| 2a/2b | host tool loop; built-in Read/Glob/Grep | answer carries tool value; stops on text-only response | 2a: 3 assistant turns for 2 tool calls, answer has TOKEN-7731 and TOKEN-2290. 2b: Glob, Grep, Read, then answer ZX-4417 | MATCH |
| 2c | parallel reads, serial writes | read starts overlap, wall < 1.8 s; write spans do not overlap | read starts 2.3 ms apart, wall 1.53 s incl. model; write spans 1.005 s each, gap 8.7 ms | MATCH |
| 3a/3b | output_schema is a final tool | capability `final_tool`; init.final_tool set; no tool_call events; success | caps output_schema final_tool; init.final_tool person; no tool_call_start; result success | MATCH |
| 3c | unsatisfiable schema, retries 2 | failed, StructuredOutputError, retry 1,2 then stop, 3 provider calls | 3 provider calls, verification retry 1, retry 2, stop; StructuredOutputError; also with a forced final tool on the wire 3 of 3 calls | MATCH |
| 4a-4c | tool_choice required / named / none | required and named call a tool; none calls none | 4a called clock, 4b called lookup, 4c no tool call (empty content) | MATCH |
| 4d/4e | named tool not offered; forced choice needs max_turns | request goes out as auto, metadata set; tenant admission -32004 without max_turns | wire tool_choice auto, metadata tool_choice_dropped lookup. Tenant: required without max_turns refused -32004 (also at run.start), with max_turns=3 opened; stdio opened without admission | MATCH |
| 5a/5b | effort levels accepted; high and max sent as xhigh | no error; wire value xhigh | 5 levels x 3 reps all success; proxy wire field reasoning_effort is xhigh for high and max (visible in the wire log, the expectation had assumed it was not) | MATCH |
| 5c | reasoning rises with effort | mean(low) < mean(medium) <= mean(xhigh) | prompt mid, 3 reps: low 175.0, medium 204.3, xhigh 204.7 completion tokens. Per-rep ranges overlap (low 166-185, xhigh 173-239) | MATCH (derived; weak) |
| 5d | thinking off | no thinking deltas, completion < 0.5x low mean | 0 thinking deltas. Prompt mid: off mean 150.3 vs 0.5x low = 87.5 (reps 6, 65, 380; median 65). Prompt p17: off 3 vs low 37 | MISMATCH on prompt mid mean (one 380-token rep); MATCH on p17 |
| 6a/6b | temperature 0 determinism; bounds | 2 of 3 identical; 2.5, -0.1, NaN fail | 3 of 3 identical text. 2.5 and -0.1: OpenError -32011; NaN: client-side ValueError before sending; 0, 1.0, 2.0 open | MATCH (NaN fails in the client JSON encoder, not at agent.open) |
| 7a/7d | unpriced cost cap; call_timeout_ms bounds | -32011 naming the model; 0 and 99 refused, 100 accepted | as expected, message names qwen3.6-35b-a3b and says set pricing or cap max_tokens; 0 and 99 refused, 100 opened; priced cap and token cap open | MATCH |
| 7b/7c | priced cost cap and token cap trip | budget exceeded, BudgetExceededError, exactly 1 provider call | 7b: 1 call, exceeded at 295 tokens, $0.000309 vs cap 1e-9. 7c: 1 call, exceeded at 295 tokens vs max_tokens 50 | MATCH |
| 8a/8c | loop guard identical; polls tool | trips at 3rd identical call; polls tool trips after more than max_idle_turns | 8a: 3 tool calls, 3 provider calls, repeated_tool_call. 8c: polls tool 5 calls (limit 4), control tool 3 calls | MATCH |
| 8b | fix-and-test not a loop | 10 patch + 10 test calls, no loop_guard | changing results: 20 calls in order, no guard, success. Constant result control: guard stop after 3 tests, subtype error | MATCH |
| 9a-9c | prerequisite, completion gate, policy with forced choice | refuse naming build; correct then stop at max_corrections; -32011 at open | 9a: deploy refused (missing build), then build, then deploy ran. 9b: corrections 1 = max_corrections 1, PolicyGateError, tool ran 0 times; with allowed corrections tool ran once. 9c: 4 combinations refused -32011 | MATCH |
| 10 | provider limiter, cap 1 | 2nd request reaches proxy only after the 1st ended; without cap both arrive together | cap=1: arrival gap 1390.7 ms, 2nd arrived 7.3 ms after the 1st ended. No cap: arrival gap 63.0 ms, 2nd arrived 2778.9 ms before the 1st ended | MATCH (1 run each; no-cap gap is 63 ms, not "within ms") |
| 11a-c | hooks deny, rewrite, post replace | block hides body, start shows rewritten input, result replaced | hook block with is_error text "evil text is not allowed"; tool_call_start input REWRITTEN-31; result ZEBRA-77, answer quotes it | MATCH |
| 12a/12b | skill, subagent | skill events; child init.tools == [Read] | skill_invoked, skill_completed, answer SKILL-TOKEN-5521; child tools [Read], answer 8842 | MATCH |
| 12c | parallel read subagents | child lifetimes overlap, parent wall < sum of children | child windows 3105 ms and 3184 ms, second started 90 ms after first; parent wall 4.85 s vs sum 6.29 s | MATCH |
| 13a/13b | capabilities, models | window 131072, source server; model found; unknown model -32036 | as expected (list has qwen3.6-35b-a3b, model_info works, ModelNotFound -32036) | MATCH |
| M1 | overhead per turn | < 100 ms | median 22.5 ms | MATCH |
| M2 | agent.open p50 | < 250 ms | 28.6 ms | MATCH |
| M3 | events per second, 200-event run | > 2000 events/s | memory 130,638 and 15,631; Postgres stream-deltas 8,759; Postgres tool-loop 430 | MATCH except Postgres tool-loop 430 events/s (MISMATCH, derived) |
| M4 | RSS idle and growth | idle < 100 MB; growth < 30 MB | idle 17,220 KB; growth 1,680 KB after 20 vLLM runs, 5,004 KB after 20 more scripted runs | MATCH |

## Measurements

| measurement | value | notes |
|---|---|---|
| M1 linch overhead per turn (1-turn text run, temperature 0, 2 completion tokens) | median 22.5 ms, range 16.8-28.7 ms | 8 of 10 pairs kept (first pair a warm-up; one pair dropped for a 1.09 s proxy latency, one for a missing proxy row). Load 3.03-3.12 |
| M1 wall, linch vs direct to the proxy | 165.9 ms vs 143.9 ms (medians) | Client-side time beyond the proxy latency: 24.4 ms (linch) vs 1.3 ms (direct) |
| M2 agent.open, fresh namespace | p50 28.6 ms, first 60.4 ms, rest 25.2-33.9 ms | n=10, load 2.76 |
| M2 agent.open, same namespace | p50 27.3 ms (25.6-34.0 ms) | n=10, load 2.76 |
| M3 events/s, stream-deltas, memory store | median 130,638 (range 66,085-138,758) | 202 events, 5 runs, scripted provider, load 2.78 |
| M3 events/s, stream-deltas, Postgres | median 8,759 (range 4,079-10,246) | 202 events, 5 runs, load 2.78 |
| M3 events/s, tool-loop, memory | median 15,631 (range 10,354-34,347) | 238 events, 5 runs, load 2.78 |
| M3 events/s, tool-loop, Postgres | median 430 (range 405-473); 0.50-0.59 s per run | 238 events, 5 runs, load 2.96 |
| M4 server RSS | idle 17,220 KB; after 1 / 5 / 10 / 20 vLLM runs 18,848 / 18,892 / 18,892 / 18,900 KB | After 20 more scripted 200-event runs: 22,224 KB. Load 2.85-3.02, 1 run per stage |
| effort, completion tokens, prompt mid (3 reps) | low 175.0, medium 204.3, high 183.3, xhigh 204.7, max 175.3, off 150.3 | max_output_tokens 600; no rep reached the cap (largest 380); `5-effort-means.jsonl` |
| effort, prompt p17 (3 reps) | low 37 (all 3 reps), off 3 (all 3 reps) | reasoning tokens 32 for low, 0 for off |
| earlier effort probe, 3 reps per cell | hard prompt: low 128.3, medium 141.0, xhigh 60.0; mid: 175.0, 211.0, 193.0; p17: 37.0, 40.3, 35.0 | `effort-probe-output.txt`; on the hard prompt xhigh used fewer tokens than low, so the ordering of 5c is not stable across prompts |
| tool-loop run | 3 provider calls, 1116 in / 83 out tokens, wall 0.946 s | 1 run (2a); tool_call duration 1 ms each |
| built-in tools run | 4 provider calls, 5860 in / 142 out tokens, wall 1.47 s | 1 run (2b) |
| skill run | 1540 and 1653 prompt tokens per call, wall 2.03 s | 1 run (12a); 3193 in / 37 out tokens in total |
| subagent run | parent 3.51 s; Subagent tool_call 2500 ms; parent 3587 in / 72 out tokens | 1 run (12b) |
| two parallel subagents | parent wall 4.85 s; children 3.105 s and 3.184 s | 1 run (12c); the proxy serialises model calls, so overlap is of lifetimes |
| budget caps, tokens at trip | 295 tokens spent (281 in + 14 out), 1 provider call | 1 run each (7b, 7c) |
| loop guard thresholds | identical call trips at 3; polls tool trips at 5 (limit 4) | 1 run each |
| provider limiter windows | cap=1 run durations 1.383 s and 1.329 s; no cap 2.842 s and 3.584 s | 1 run each; arrival gap 1390.7 ms vs 63.0 ms |
| pricing check | reported minus expected cost: 2e-21 and 0 USD | 2 runs (1c) |

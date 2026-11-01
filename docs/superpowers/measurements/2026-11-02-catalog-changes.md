# Catalog change measurements, 2026-11-02

Wire-level runs of `scripts/measure_catalog_changes.py` and equivalents. Not measured: Anthropic,
OpenAI Responses, llama.cpp.

## DeepSeek (deepseek-v4-flash, chat completions)

Cache hits come from usage.prompt_cache_hit_tokens. padding=400, short history.

| Scenario | Cached | Total |
|---|---|---|
| control | 4864 | 5036 |
| tail_text | 4864 | 5060 |
| prefix_append | 4736 | 5092 |
| prefix_middle | 4480 | 5096 |
| prefix_edit | 4480 | 5034 |

Hits stop near the changed tool, so this provider appears to render system before tools.
The history is short here, so these rows do not show what a long session loses.

## SGLang (self-hosted)

/v1/models reports owned_by sglang, so this is SGLang, not vLLM. usage.prompt_tokens_details
is null and /metrics is 404, so evidence is latency only. 78,226 prompt tokens, max_tokens=1,
eval traffic possibly sharing the server.

| Request | Seconds |
|---|---|
| Cold | about 6.4 to 7.9 |
| Warm repeat | about 1.4 |
| control, later turn | 1.29 to 1.46 in the clean run |
| tail_text | 1.36 to 1.42 |
| prefix_append | 6.52 to 7.07 |
| prefix_middle | 6.46 to 6.54 |
| prefix_edit | 6.49 to 6.54 |

First run's control rows 9.8 and 9.04, and one warm 8.44 in the second run, are treated as
contention or eviction. Appending a tool cost the same as a middle insert on this template.

## vLLM (qwen3.6-35b-a3b)

owned_by vllm. block_size 2176, so hits come in whole blocks. chat_template_kwargs
enable_thinking=false, max_tokens=1. The cached field equals the /metrics hit delta in
every row.

| Scenario | Prompt | Cached | Warm s | Measured s |
|---|---|---|---|---|
| control | 39077 | 34816 | 0.72 | 0.60 |
| tail_text | 39101 | 34816 | 0.65 | 0.59 |
| prefix_append | 39145 | 0 | 0.69 | 1.34 |
| prefix_middle | 39145 | 0 | 0.73 | 1.34 |
| prefix_edit | 39077 | 0 | 0.69 | 1.34 |

At 5.9k prompt tokens the same pattern holds: 2176 cached versus 0.
Any tool change drops hits to 0, including an append. The change falls inside the first
block, so tools render ahead of the large system text on this template, or within block 1.

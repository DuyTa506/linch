"""Cost arithmetic of s2 (and any scenario with usage events + gateway records): cost_check.py <live|rehearsal> <scenario>"""

import json
import sys

data = json.load(open(f"/home/aiqk3/Desktop/duy_dev/linch-agent-scratch/e2e/results/deepseek/{sys.argv[1]}/{sys.argv[2]}.json"))
IN, OUT, HIT = 0.3, 1.2, 0.006  # USD per million tokens (deepseek-flash peak, docs/usage/monitoring.md)
records = [r for r in data["gateway"] if r.get("kind") == "forwarded" and r.get("status") == 200]
events = [e for t in data.get("turns", []) for e in t["events"]] if "turns" in data else data.get("usage", [])
total_linch = 0.0
total_expected = 0.0
for index, (record, event) in enumerate(zip(records, events, strict=False)):
    u = record["usage"]
    hit, miss, out = u["prompt_cache_hit_tokens"], u["prompt_cache_miss_tokens"], u["completion_tokens"]
    expected = (miss * IN + hit * HIT + out * OUT) / 1e6
    linch = event["cost_usd"]
    total_linch += linch
    total_expected += expected
    reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    print(
        f"call {index + 1}: prompt={u['prompt_tokens']} hit={hit} miss={miss} completion={out} reasoning={reasoning} "
        f"hit_ratio={hit / u['prompt_tokens']:.4f}\n  linch usage={event['usage']}\n"
        f"  expected={expected!r} linch={linch!r} diff={abs(expected - linch):.3e} ok={abs(expected - linch) <= 1e-9}"
    )
print(f"sum expected={total_expected!r} sum linch={total_linch!r} diff={abs(total_expected - total_linch):.3e}")
for turn in data.get("turns", []):
    print("result total_cost_usd", turn["result"].get("total_cost_usd"), "stop", turn["result"].get("subtype"))

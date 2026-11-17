"""Cross-scenario summary of the live run: summary.py"""

import json
import os
import tempfile
from pathlib import Path

live = (
    Path(os.environ.get("E2E_RESULTS", Path(tempfile.gettempdir()) / "linch-live-e2e"))
    / "deepseek"
    / "live"
)
rows = [json.loads(line) for line in (live / "gateway.jsonl").read_text().splitlines() if line]
forwarded = [r for r in rows if r["kind"] == "forwarded"]
print(
    "forwarded",
    len(forwarded),
    "by status",
    {s: sum(1 for r in forwarded if r["status"] == s) for s in {r["status"] for r in forwarded}},
)
print(
    "mock",
    sum(1 for r in rows if r["kind"] == "mock"),
    "echo400",
    sum(1 for r in rows if r["kind"] == "echo400"),
    "refused",
    sum(1 for r in rows if r["kind"].startswith("refused")),
)
probe_missing = [r["seq"] for r in forwarded if not r["probe_header_present"]]
print("forwarded without x-e2e-probe:", probe_missing, "(seq 8 is the raw probe)")
print(
    "authorization header name present on all forwarded:",
    all("authorization" in r["header_names"] for r in forwarded),
)
print("max_tokens seen:", sorted({r["max_tokens"] for r in forwarded}))
print("response models:", {r.get("response_model") for r in forwarded})
for tag in ("s3_r1", "s3_r2", "s3_r3"):
    d = json.loads((live / f"{tag}.json").read_text())
    a = d["analysis"]
    req = {r["label"]: r for r in a["requests"]}
    comp = [c for t in d["turns"] for c in t.get("compaction", [])][0]
    b_text = d["turns"][5]["assistant_text"]
    print(
        tag,
        "A prompt",
        req["A"]["usage"]["prompt_tokens"],
        "hitA",
        req["A"]["usage"]["prompt_cache_hit_tokens"],
        "| summary prompt",
        req["summary"]["usage"]["prompt_tokens"],
        "hit",
        req["summary"]["usage"]["prompt_cache_hit_tokens"],
        f"ratio {req['summary']['hit_ratio']:.4f}",
        "compl",
        req["summary"]["usage"]["completion_tokens"],
        "| B prompt",
        req["B_primary"]["usage"]["prompt_tokens"],
        f"| C ratio {req['C']['hit_ratio']:.4f}",
        "| prefix identical",
        a["summary_prefix_identical_to_A"],
        "extra",
        a["summary_extra_after_A"],
        "| tokens",
        comp["tokens_before"],
        "->",
        comp["tokens_after"],
        "verbatim",
        req["B_primary"]["verbatim_blocks"],
        f"| gap_s {a['summary_gap_s']:.2f}",
        "| B answer",
        json.dumps(b_text)[:90],
    )

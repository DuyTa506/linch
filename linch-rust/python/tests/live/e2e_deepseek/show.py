"""Print a scenario result compactly: show.py <live|rehearsal> <scenario> [key ...]"""

import json
import os
import sys
import tempfile
from pathlib import Path

root = (
    Path(os.environ.get("E2E_RESULTS", Path(tempfile.gettempdir()) / "linch-live-e2e")) / "deepseek"
)
data = json.loads((root / sys.argv[1] / f"{sys.argv[2]}.json").read_text())
keys = sys.argv[3:] or list(data)
for key in keys:
    value = data[key]
    if key == "gateway":
        for r in value:
            print(
                "GW",
                {
                    k: r.get(k)
                    for k in (
                        "kind",
                        "status",
                        "seq",
                        "tool_choice",
                        "tools",
                        "has_response_format",
                        "max_tokens",
                        "thinking",
                        "reasoning_effort",
                        "probe_header_present",
                        "usage",
                        "finish_reason",
                        "response_model",
                        "error_body",
                    )
                },
            )
        continue
    print(key, json.dumps(value, default=str)[:1800])

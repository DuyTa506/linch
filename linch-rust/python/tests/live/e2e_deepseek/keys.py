"""Print the analysis block of a scenario result: keys.py <live|rehearsal> <scenario>"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = (
    Path(os.environ.get("E2E_RESULTS", Path(tempfile.gettempdir()) / "linch-live-e2e")) / "deepseek"
)
d = json.load(open(ROOT / sys.argv[1] / f"{sys.argv[2]}.json"))
for k, v in d["analysis"].items():
    if k != "requests":
        print(k, v)

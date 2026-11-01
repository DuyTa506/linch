#!/usr/bin/env python3
"""Reproduce worker exit during a three-second Postgres proxy outage.

Requires the dedicated local Postgres and toxiproxy-server used by load_test.py.
Extra arguments override the point defaults, for example --output /tmp/repro.json.
"""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    options = (
        "point --workers 2 --rate 2 --warmup 2 --duration 12 --drain-timeout 15 "
        "--rtt 1 --pool-size 8 --readers 0 --chaos blip "
        "--output /tmp/wp18-db-blip-repro.json"
    ).split()
    command = [
        "flock",
        "--timeout",
        "900",
        "/tmp/wp8-cpu.lock",
        "timeout",
        "--signal=INT",
        "--kill-after=15s",
        "75s",
        sys.executable,
        str(Path(__file__).with_name("load_test.py")),
        *options,
        *sys.argv[1:],
    ]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())

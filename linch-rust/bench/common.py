"""Helpers shared by the two Python sides (`py_side.py`, `rs_client_side.py`).

Standard library only. A side reads one job (JSON, from the file named on
its command line) and prints one result object as the last line of stdout;
`run_all.py` turns those into the table.
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

Job = dict[str, Any]
Result = dict[str, Any]


def load_job() -> Job:
    with open(sys.argv[1]) as handle:
        return json.load(handle)


def load_turns(path: str) -> list[dict]:
    with open(path) as handle:
        return json.load(handle)["turns"]


def peak_rss_mb() -> float:
    """This process's peak RSS (`ru_maxrss` is KiB on Linux)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def proc_cpu_ns(pid: int) -> int:
    """CPU nanoseconds used so far by process `pid`, all its threads.

    Reads the process's CPU-time clock (what glibc's `clock_getcpuclockid`
    returns: `MAKE_PROCESS_CPUCLOCK(pid, CPUCLOCK_SCHED)`), which Linux lets
    any process read; falls back to the live threads' schedstat, which
    advances only at scheduler ticks.
    """
    try:
        return time.clock_gettime_ns(((~pid) << 3) | 2)
    except OSError:
        pass
    total = 0
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return 0
    for task in tasks:
        try:
            with open(f"/proc/{pid}/task/{task}/schedstat") as handle:
                total += int(handle.read().split()[0])
        except (OSError, ValueError, IndexError):
            pass
    return total


def proc_hwm_mb(pid: int) -> float | None:
    """Peak RSS of another live process (`VmHWM`), in MiB."""
    try:
        with open(f"/proc/{pid}/status") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        return None
    return None


class Clock:
    """Wall per iteration and CPU over all timed iterations.

    `extra_pids` adds the CPU of other processes (the rs-client sidecar).
    """

    def __init__(self, extra_pids: Callable[[], list[int]] = list) -> None:
        self.wall_ms: list[float] = []
        self.cpu_ms = 0.0
        self.other_cpu_ms = 0.0  # the part of cpu_ms spent in `extra_pids`
        self.last: list = []  # the last timed run's events, for checks
        self._extra = extra_pids
        self._start = 0.0
        self._own = 0.0
        self._other = 0.0

    def _other_ms(self) -> float:
        return sum(proc_cpu_ns(pid) for pid in self._extra()) / 1e6

    # Reading another process's CPU clock costs this process CPU, so the
    # reads sit outside the measured window: before our own clock at the
    # start, after it at the stop.
    def start(self) -> None:
        self._other = self._other_ms()
        self._own = time.process_time() * 1000
        self._start = time.perf_counter()

    def stop(self) -> None:
        self.wall_ms.append(1000 * (time.perf_counter() - self._start))
        own = time.process_time() * 1000 - self._own
        other = self._other_ms() - self._other
        self.cpu_ms += own + other
        self.other_cpu_ms += other


def ok(clock: Clock, ops: float, op: str, rss_mb: float, **extra: Any) -> Result:
    result = {
        "status": "ok",
        "iterations": len(clock.wall_ms),
        "wall_ms": clock.wall_ms,
        "cpu_ms": clock.cpu_ms,
        "ops_per_iter": ops,
        "op": op,
        "peak_rss_mb": rss_mb,
        **extra,
    }
    if clock.other_cpu_ms:
        result["sidecar_cpu_ms"] = clock.other_cpu_ms
    return result


def na(reason: str) -> Result:
    return {"status": "n/a", "reason": reason}


def main(cases: dict[str, Callable[[Job], Awaitable[Result]]], runner: Callable[..., Any]) -> None:
    """Run the job's case and print its result as JSON."""
    job = load_job()
    handler = cases.get(job["case"])
    try:
        if handler is None:
            result = na("not implemented on this side")
        else:
            result = runner(handler(job))
    except Exception as error:  # noqa: BLE001 - reported in the table, with the traceback on stderr
        traceback.print_exc()
        result = {"status": "error", "error": f"{type(error).__name__}: {error}"[:300]}
    print(json.dumps(result))


def bench_dir() -> Path:
    return Path(__file__).resolve().parent

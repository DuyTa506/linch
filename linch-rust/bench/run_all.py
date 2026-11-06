"""Python Linch versus linch-rust: run the bench cases and write one table.

    python run_all.py --out DIR [--quick] [--cases 1,3,loop] [--sides py,rs-kernel]
                      [--pg-dsn DSN] [--live] [--runs N]

Three sides run the same work from the same fixture files:

- `py`        Python Linch (master), `py_side.py` on `--py-python`;
- `rs-client` the `linch_rust` client over the `linch-server` sidecar,
              `rs_client_side.py` on `--client-python`;
- `rs-kernel` Rust direct through the facade crate, the `bench_driver`
              example (`cargo build --release -p linch --example bench_driver`).

Each (case, side) runs in its own process. DIR gets `raw/<side>.json` (every
result as the side printed it), `logs/` (each process's stderr), `jobs/`
and `results.md`. A cell a side cannot run says `n/a (reason)`.

Every side process gets this process's environment without provider keys
(`*_API_KEY` and the like). Only `--live` passes `DEEPSEEK_API_KEY` on, to
the live case, and nothing here prints it. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # linch-rust/
FIXTURES = HERE / "fixtures"
SIDES = ("py", "rs-client", "rs-kernel")

MODEL = "scripted-model"
SYSTEM = "You are the Linch benchmark agent."
ECHO = {
    "name": "Echo",
    "description": "Echo the value.",
    "input_schema": {"type": "object", "properties": {"value": {"type": "string"}}},
}
LIVE_PROMPTS = [
    "What is the weather in Paris right now?",
    "What is the weather in Tokyo right now?",
    "What is the weather in Lima right now?",
]
LIVE_SYSTEM = "Use the get_weather tool for weather questions, then answer in one short sentence."
WEATHER = {
    "name": "get_weather",
    "description": "Get the current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name"}},
        "required": ["city"],
    },
}
LOOPS = ("10x1", "10x3", "50x1", "1x8")
TOOLS = ("Read", "Write", "Edit", "Grep", "Glob", "Bash", "Grep-2k")
VENDORS = ("deepseek", "openai-chat", "openai-responses", "anthropic", "gemini")

# The fixed grid: (number, case, variants, title). The table always shows
# every cell of it, whatever the sides returned.
GRID = [
    (1, "loop", LOOPS, "Loop shapes, memory stores"),
    (2, "compaction", ("summary",), "Compaction"),
    (3, "pg_loop", LOOPS, "Loop shapes, Postgres stores"),
    (4, "tools", TOOLS, "Workspace tools"),
    (5, "mcp", ("echo",), "MCP stdio round trip"),
    (6, "decode", VENDORS, "Provider stream decoding"),
    (7, "worker", ("postgres",), "Worker queue"),
    (8, "workflow", ("memory", "postgres"), "Workflows"),
    (9, "evals", ("scripted",), "Evals"),
    (10, "startup", ("cold",), "Startup"),
    (11, "concurrency", ("10x1",), "Concurrent sessions"),
    (12, "rpc_overhead", LOOPS, "RPC overhead (rs-client minus rs-kernel)"),
    (13, "live", ("deepseek",), "Live DeepSeek"),
]
PG_CASES = {"pg_loop", "worker"}
SECRET = re.compile(
    r"(_API_KEY|_TOKEN|_SECRET|_PASSWORD)$|^(ANTHROPIC|OPENAI|GEMINI|GOOGLE|DEEPSEEK)_"
)


def scrubbed_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not SECRET.search(key)}


def git_head(path: Path) -> str | None:
    """The commit checked out at `path`, read from the git files (no git call)."""
    for directory in (path, *path.parents):
        dot = directory / ".git"
        if dot.is_file():
            gitdir = Path(dot.read_text().split("gitdir:", 1)[1].strip())
        elif dot.is_dir():
            gitdir = dot
        else:
            continue
        head = (gitdir / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        common = gitdir / "commondir"
        base = (gitdir / common.read_text().strip()).resolve() if common.is_file() else gitdir
        for root in (gitdir, base):
            if (root / ref).is_file():
                return (root / ref).read_text().strip()
        packed = base / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                if line.endswith(" " + ref):
                    return line.split()[0]
        return None
    return None


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out = Path(args.out).resolve()
        for sub in ("raw", "logs", "jobs", "work"):
            (self.out / sub).mkdir(parents=True, exist_ok=True)
        self.iterations = 2 if args.quick else args.runs
        self.warmup = 1
        self.results: dict[str, dict[str, dict]] = {side: {} for side in SIDES}
        self.sse: subprocess.Popen | None = None
        self.sse_url = ""

    # ------------------------------------------------------------ commands

    def command(self, side: str, job_path: Path) -> list[str]:
        if side == "py":
            return [self.args.py_python, "-I", str(HERE / "py_side.py"), str(job_path)]
        if side == "rs-client":
            return [self.args.client_python, "-I", str(HERE / "rs_client_side.py"), str(job_path)]
        return [self.args.kernel_bin, str(job_path)]

    def unavailable(self, side: str) -> str | None:
        binary = {"rs-client": self.args.server_bin, "rs-kernel": self.args.kernel_bin}.get(side)
        if binary is not None and not Path(binary).is_file():
            return f"binary not built: {binary}"
        return None

    def pg_admin(self, action: str, schema: str) -> dict | None:
        proc = subprocess.run(
            [
                self.args.py_python,
                "-I",
                str(HERE / "py_side.py"),
                "--pg-admin",
                action,
                self.args.pg_dsn,
                schema,
            ],
            capture_output=True,
            text=True,
            env=scrubbed_env(),
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pg-admin {action} failed: {proc.stderr.strip()[-300:]}")
        return json.loads(proc.stdout) if action == "stats" else None

    def pg_writes(self, schema: str) -> dict:
        # Backends flush their table stats when they exit; wait until two
        # reads agree.
        last = None
        for _ in range(20):
            stats = self.pg_admin("stats", schema)
            if stats == last:
                break
            last = stats
            time.sleep(0.25)
        return last or {}

    # ---------------------------------------------------------------- jobs

    def base_job(self, case: str, variant: str) -> dict:
        return {
            "case": case,
            "variant": variant,
            "iterations": self.iterations,
            "warmup": self.warmup,
            "model": MODEL,
            "system": SYSTEM,
            "tool": ECHO,
            "max_tool_concurrency": 8,
            "stores": "memory",
            "server_bin": self.args.server_bin,
        }

    def job(self, side: str, case: str, variant: str) -> dict | str:
        """The job for one cell, or why the cell cannot run."""
        job = self.base_job(case, variant)
        if case in ("loop", "pg_loop"):
            job["fixture"] = str(FIXTURES / f"loop_{variant}.json")
            if case == "pg_loop":
                job["stores"] = "postgres"
        elif case == "live":
            if not self.args.live:
                return "live run off; pass --live"
            if "DEEPSEEK_API_KEY" not in os.environ:
                return "DEEPSEEK_API_KEY not set"
            job.update(
                model="deepseek-flash", system=LIVE_SYSTEM, tool=WEATHER, prompts=LIVE_PROMPTS
            )
        else:
            return "not wired yet"
        return job

    def env_for(self, case: str) -> dict[str, str]:
        env = scrubbed_env()
        if case == "live" and "DEEPSEEK_API_KEY" in os.environ:
            env["DEEPSEEK_API_KEY"] = os.environ["DEEPSEEK_API_KEY"]
        return env

    def run_cell(self, side: str, case: str, variant: str) -> dict:
        reason = self.unavailable(side)
        if reason is not None:
            return {"status": "n/a", "reason": reason}
        job = self.job(side, case, variant)
        if isinstance(job, str):
            return {"status": "n/a", "reason": job}
        name = f"{side}__{case}__{variant}"
        schema = None
        if job.get("stores") == "postgres" or case in PG_CASES:
            if not self.args.pg_dsn:
                return {"status": "n/a", "reason": "no --pg-dsn given"}
            schema = re.sub(r"[^a-z0-9_]", "_", f"bench_{name}_{os.getpid()}".lower())
            self.pg_admin("drop", schema)
            self.pg_admin("create", schema)
            sep = "&" if "?" in self.args.pg_dsn else "?"
            job["pg_dsn"] = f"{self.args.pg_dsn}{sep}options=-csearch_path%3D{schema}"
            job["stores"] = "postgres"
        job_path = self.out / "jobs" / f"{name}.json"
        job_path.write_text(json.dumps(job, indent=2))
        log = self.out / "logs" / f"{name}.log"
        started = time.perf_counter()
        result: dict = {"status": "error", "error": "the side process did not start"}
        try:
            with log.open("w") as stderr:
                proc = subprocess.run(
                    self.command(side, job_path),
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    text=True,
                    env=self.env_for(case),
                    timeout=self.args.timeout,
                    cwd=self.out / "work",
                )
            lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
            if not lines:
                result = {
                    "status": "error",
                    "error": f"exit {proc.returncode}, no result (see logs/{log.name})",
                }
            else:
                result = json.loads(lines[-1])
        except subprocess.TimeoutExpired:
            result = {"status": "error", "error": f"timed out after {self.args.timeout} s"}
        except OSError as error:
            result = {"status": "error", "error": f"cannot start the side: {error}"}
        finally:
            if schema is not None:
                try:
                    result["pg_tuple_writes"] = self.pg_writes(schema)
                finally:
                    self.pg_admin("drop", schema)
        result["process_wall_s"] = time.perf_counter() - started
        return result

    # ------------------------------------------------------------- derived

    def derive_rpc(self, variant: str) -> dict:
        client = self.results["rs-client"].get(f"loop/{variant}", {})
        kernel = self.results["rs-kernel"].get(f"loop/{variant}", {})
        if client.get("status") != "ok" or kernel.get("status") != "ok":
            return {
                "status": "n/a",
                "reason": "needs loop/" + variant + " on rs-client and rs-kernel",
            }
        wall = statistics.median(client["wall_ms"]) - statistics.median(kernel["wall_ms"])
        cpu = client["cpu_ms"] / client["iterations"] - kernel["cpu_ms"] / kernel["iterations"]
        return {
            "status": "ok",
            "derived": True,
            "wall_ms": [wall],
            "cpu_ms": cpu,
            "iterations": 1,
            "ops_per_iter": client["ops_per_iter"],
            "op": "turn",
            "peak_rss_mb": client["peak_rss_mb"] - kernel["peak_rss_mb"],
        }

    # ----------------------------------------------------------------- run

    def selected(self, number: int, case: str) -> bool:
        wanted = self.args.cases
        return not wanted or str(number) in wanted or case in wanted

    def run(self) -> None:
        sides = [side for side in SIDES if not self.args.sides or side in self.args.sides]
        try:
            for number, case, variants, _ in GRID:
                if not self.selected(number, case):
                    continue
                for variant in variants:
                    for side in SIDES:
                        key = f"{case}/{variant}"
                        if side not in sides:
                            continue
                        if case == "rpc_overhead":
                            result = (
                                self.derive_rpc(variant)
                                if side == "rs-client"
                                else {"status": "n/a", "reason": "shown on the rs-client row"}
                            )
                        else:
                            print(f"[{side}] {key} ...", file=sys.stderr, flush=True)
                            result = self.run_cell(side, case, variant)
                            print(f"[{side}] {key}: {summary(result)}", file=sys.stderr, flush=True)
                        self.results[side][key] = result
        finally:
            if self.sse is not None:
                self.sse.terminate()
                self.sse.wait()
        self.write(sides)

    def provenance(self) -> dict:
        return {
            "quick": self.args.quick,
            "iterations": self.iterations,
            "warmup": self.warmup,
            "bench_commit": git_head(HERE),
            "py_python": self.args.py_python,
            "client_python": self.args.client_python,
            "server_bin": self.args.server_bin,
            "kernel_bin": self.args.kernel_bin,
            "host": socket.gethostname(),
            "cpus": os.cpu_count(),
            "loadavg": os.getloadavg(),
            "date": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        }

    def write(self, sides: list[str]) -> None:
        prov = self.provenance()
        for side in sides:
            raw = {"side": side, "provenance": prov, "results": self.results[side]}
            (self.out / "raw" / f"{side}.json").write_text(json.dumps(raw, indent=2))
        (self.out / "results.md").write_text(table(self.results, sides, prov, self.args))


# ------------------------------------------------------------------ table


def summary(result: dict) -> str:
    if result.get("status") != "ok":
        return f"{result.get('status')}: {result.get('reason') or result.get('error')}"
    return f"{per_op(result)}, wall {statistics.median(result['wall_ms']):.3f} ms"


def per_op(result: dict) -> str:
    wall = statistics.median(result["wall_ms"])
    ops, op = result["ops_per_iter"], result["op"]
    if op == "event":
        mb = result.get("bytes_per_iter", 0) / 1e6
        return f"{mb / (wall / 1000):.1f} MB/s, {ops / (wall / 1000) / 1000:.1f} k ev/s"
    if op in ("job", "run"):
        return f"{ops / (wall / 1000):.1f} {op}s/s"
    return f"{wall / ops:.3f} ms/{op}"


def writes(result: dict) -> str:
    parts = []
    total = result.get("runs_total")
    pg = result.get("pg_tuple_writes")
    if pg and total:
        tuples = pg.get("ins", 0) + pg.get("upd", 0) + pg.get("del", 0)
        parts.append(f"pg {tuples / total:.1f} tuples/run")
    counted = result.get("writes_per_run")
    if counted:
        for store, calls in counted.items():
            if calls:
                parts.append(f"{store} {sum(calls.values())} calls/run")
    return "; ".join(parts) if parts else "n/c"


def table(results: dict, sides: list[str], prov: dict, args: argparse.Namespace) -> str:
    lines = [
        "# Python Linch vs linch-rust",
        "",
        "Smoke run (`--quick`): checks the harness only; these are not results."
        if args.quick
        else f"{prov['iterations']} timed iterations after {prov['warmup']} warm-up per cell.",
        "",
        f"Bench commit {prov['bench_commit']}, {prov['date']}, {prov['cpus']} CPUs, "
        f"load average {', '.join(f'{x:.1f}' for x in prov['loadavg'])}.",
        "",
        "Wall is the median per iteration, CPU the mean per iteration (rs-client includes the "
        "sidecar), RSS the process peak (rs-client: client plus sidecar).",
        "",
        "| # | Case | Side | Wall ms | CPU ms | Per op | Peak RSS MB | Store writes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for number, case, variants, title in GRID:
        if args.cases and str(number) not in args.cases and case not in args.cases:
            continue
        for variant in variants:
            for side in sides:
                result = results[side].get(f"{case}/{variant}")
                name = f"{title}: {variant}"
                if result is None:
                    continue
                if result.get("status") == "ok":
                    wall = statistics.median(result["wall_ms"])
                    cpu = result["cpu_ms"] / max(result["iterations"], 1)
                    cells = [f"{wall:.3f}", f"{cpu:.3f}", per_op(result)]
                    cells += [f"{result['peak_rss_mb']:.1f}", writes(result)]
                    lines.append(f"| {number} | {name} | {side} | {' | '.join(cells)} |")
                else:
                    why = result.get("reason") or result.get("error") or "?"
                    label = "n/a" if result.get("status") == "n/a" else "error"
                    why = why.replace("|", "/").replace("\n", " ")
                    lines.append(f"| {number} | {name} | {side} | {label} ({why}) | | | | |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--quick", action="store_true", help="smoke run: 2 timed iterations")
    parser.add_argument(
        "--runs", type=int, default=20, help="timed iterations per cell (default 20)"
    )
    parser.add_argument("--cases", default="", help="comma list of case numbers or names")
    parser.add_argument("--sides", default="", help="comma list of py, rs-client, rs-kernel")
    parser.add_argument("--pg-dsn", default="", help="Postgres DSN; each cell gets its own schema")
    parser.add_argument(
        "--live", action="store_true", help="run the live DeepSeek case (off by default)"
    )
    parser.add_argument(
        "--py-python", default=sys.executable, help="interpreter with master's linch"
    )
    parser.add_argument(
        "--client-python", default="", help="interpreter for rs-client (default --py-python)"
    )
    parser.add_argument("--server-bin", default=str(ROOT / "target" / "release" / "linch-server"))
    parser.add_argument(
        "--kernel-bin", default=str(ROOT / "target" / "release" / "examples" / "bench_driver")
    )
    parser.add_argument(
        "--mcp-server-bin",
        default=str(ROOT / "target" / "release" / "examples" / "mcp_stdio_server"),
    )
    parser.add_argument("--timeout", type=float, default=900, help="seconds per cell")
    args = parser.parse_args()
    args.cases = {c.strip() for c in args.cases.split(",") if c.strip()}
    args.sides = {s.strip() for s in args.sides.split(",") if s.strip()}
    args.client_python = args.client_python or args.py_python
    unknown = args.sides - set(SIDES)
    if unknown:
        parser.error(f"unknown sides: {', '.join(sorted(unknown))}")
    work = Path(args.out).resolve() / "work"
    if work.exists():
        shutil.rmtree(work)
    Runner(args).run()


if __name__ == "__main__":
    main()

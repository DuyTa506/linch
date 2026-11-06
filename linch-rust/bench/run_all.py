"""Python Linch versus linch-rust: run the bench cases and write one table.

    python run_all.py --out DIR [--quick] [--cases 1,3,loop] [--sides py,rs-kernel]
                      [--pg-dsn DSN] [--live] [--runs N]
                      [--sessions 100,500] [--sidecars N] [--turn-delay S]

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
import math
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import threading
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
    (11, "concurrency", ("16-sessions-10x1",), "Concurrent sessions"),
    (12, "rpc_overhead", LOOPS, "RPC overhead (rs-client minus rs-kernel)"),
    (13, "live", ("deepseek",), "Live DeepSeek"),
    (14, "sessions", (), "Concurrent sessions, Postgres"),  # variants: `variants_of`
]
PG_CASES = {"pg_loop", "worker"}
COMPACTION = {
    # Rust compacts before the provider view reaches this many tokens (its
    # estimate: text, tool inputs and results at 4 characters a token).
    "rs_threshold_tokens": 25_000,
    # Python compacts when estimate + reserve (a fifth of the window) reaches
    # 80% of the window: 0.6 x 42 833 = 25 700 tokens of its estimate, which
    # also counts the JSON of each message, so both cross at the same call.
    "py_context_window": 42_833,
    "summary_prompt": "Summarize the bench conversation so far.",
}
# variant: (fixture, tool calls per run)
TOOL_FIXTURES = {
    "Read": ("tools_read.json", 10),
    "Write": ("tools_write.json", 10),
    "Edit": ("tools_edit.json", 20),
    "Grep": ("tools_grep.json", 10),
    "Glob": ("tools_glob.json", 10),
    "Bash": ("tools_bash.json", 10),
    "Grep-2k": ("tools_grep2k.json", 5),
}
CONCURRENT_SESSIONS = 16
# Case 14: sessions per cell; turns per run (Bash, TaskCreate, TaskList,
# text: `chat_server.py`); Postgres connections per side in total on the
# row with the default caps raised.
SESSIONS = (100, 500, 1000, 2000)
QUICK_SESSIONS = (20, 50)
SESSION_TURNS = 4
RAISED_PG_POOL = 40
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_MB = os.sysconf("SC_PAGE_SIZE") / 2**20
LIVE = {
    "model": "deepseek-flash",
    "system": LIVE_SYSTEM,
    "tool": WEATHER,
    "tool_reply": "Sunny, 21 C, light wind in {city}.",
    "prompts": LIVE_PROMPTS,
    "spec": {"thinking": {"type": "disabled"}, "max_turns": 4, "max_output_tokens": 1024},
}
DUMMY_KEYS = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")
NO_RPC = {
    "py": {"status": "n/a", "reason": "no sidecar: Python runs in process"},
    "rs-kernel": {"status": "n/a", "reason": "the floor; the difference is on the rs-client row"},
}
SECRET = re.compile(
    r"(_API_KEY|_TOKEN|_SECRET|_PASSWORD)$|^(ANTHROPIC|OPENAI|GEMINI|GOOGLE|DEEPSEEK)_"
)


def scrubbed_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not SECRET.search(key)}


def build_workspace(root: Path) -> None:
    """The tool workspace: `src/` (10 small files) and `tree/` (2000 files).

    Every `src` file has a unique `marker_<i>` for Edit; even ones contain
    `needle`. In `tree/`, every 50th file contains `needle_hit`.
    """
    src = root / "src"
    src.mkdir(parents=True)
    for i in range(10):
        lines = [f"line {n}: alpha beta gamma delta {i}" for n in range(40)]
        lines.insert(20, f"marker_{i}")
        if i % 2 == 0:
            lines.insert(30, "a needle in the file")
        (src / f"file_{i:02d}.txt").write_text("\n".join(lines) + "\n")
    for d in range(20):
        folder = root / "tree" / f"d{d:02d}"
        folder.mkdir(parents=True)
        for f in range(100):
            n = d * 100 + f
            lines = [f"row {r} of file {n}: lorem ipsum dolor sit amet" for r in range(30)]
            if n % 50 == 0:
                lines.insert(15, "here is the needle_hit")
            (folder / f"f{f:03d}.txt").write_text("\n".join(lines) + "\n")


def variants_of(case: str, variants: tuple, args: argparse.Namespace) -> tuple:
    """A case's variants. Case 14's come from the session counts: M on every
    side, `Mx<N>` over N sidecars (rs-client only), and the largest M with
    the default caps raised (`+caps`)."""
    if case != "sessions":
        return variants
    sizes = args.sessions or (QUICK_SESSIONS if args.quick else SESSIONS)
    each = [v for m in sizes for v in (f"{m}", f"{m}x{args.sidecars}")]
    return (*each, f"{sizes[-1]}+caps")


def pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile of `values` (not empty)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def tree_usage(root: int) -> dict:
    """CPU seconds, RSS, descendants and open fds of the process tree under `root`.

    CPU is each live process's own time plus that of the children it reaped
    (`cutime`, `cstime`), so a Bash child that exited between reads counts.
    A `linch-server` is not dumpable, so its `fd/` cannot be listed: its fd
    table size (`FDSize`, at least its open fds) is counted instead and
    `fds_bound` is set.
    """
    ticks = pages = fds = processes = 0
    bound = False
    todo = [root]
    while todo:
        pid = todo.pop()
        try:
            with open(f"/proc/{pid}/stat") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
            tasks = os.listdir(f"/proc/{pid}/task")
            try:
                open_fds = len(os.listdir(f"/proc/{pid}/fd"))
            except PermissionError:  # not dumpable, or a zombie (FDSize 0)
                with open(f"/proc/{pid}/status") as handle:
                    slots = [line for line in handle if line.startswith("FDSize:")]
                open_fds = int(slots[0].split()[1])
                bound = bound or open_fds > 0
        except (OSError, IndexError):  # it exited while being read
            continue
        ticks += sum(int(value) for value in fields[11:15])  # utime stime cutime cstime
        pages += int(fields[21])
        fds += open_fds
        processes += 1
        for task in tasks:
            try:
                with open(f"/proc/{pid}/task/{task}/children") as handle:
                    todo.extend(int(child) for child in handle.read().split())
            except OSError:
                pass
    return {
        "cpu_s": ticks / CLK_TCK,
        "peak_rss_mb": pages * PAGE_MB,
        "peak_children": processes - 1,
        "peak_fds": fds,
        "fds_bound": bound,
    }


class Sampler(threading.Thread):
    """A process tree's peaks, read every 100 ms between `begin` and `end`,
    and its CPU seconds from one to the other."""

    def __init__(self, root: int) -> None:
        super().__init__(daemon=True)
        self.root = root
        self.peaks = {"peak_rss_mb": 0.0, "peak_children": 0, "peak_fds": 0, "fds_bound": False}
        self.cpu_s = 0.0
        self.stopped = threading.Event()

    def read(self) -> float:
        usage = tree_usage(self.root)
        for key in self.peaks:
            self.peaks[key] = max(self.peaks[key], usage[key])
        return usage["cpu_s"]

    def begin(self) -> None:
        self.cpu_s = self.read()
        self.start()

    def run(self) -> None:
        while not self.stopped.wait(0.1):
            self.read()

    def end(self) -> dict:
        self.stopped.set()
        self.join()
        return {"cpu_s": self.read() - self.cpu_s, **self.peaks}


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
        self.chat: subprocess.Popen | None = None  # case 14's chat server, one per cell
        self.chat_stats = Path()

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
        return json.loads(proc.stdout) if action in ("stats", "commits") else None

    def pg_commits(self, start: int) -> int:
        """The database's commits since `start`, once the side's backends have
        flushed their stats (on exit). A read itself commits a fixed number
        (its connection, its query): reads repeat until two steps agree, and
        that step comes off for each read, the start read included."""
        reads = [self.pg_admin("commits", "")]
        while len(reads) < 20:
            time.sleep(0.25)
            reads.append(self.pg_admin("commits", ""))
            if len(reads) >= 3 and reads[-1] - reads[-2] == reads[-2] - reads[-3]:
                break
        return reads[-1] - start - (reads[-1] - reads[-2]) * len(reads)

    def pg_writes(self, schema: str) -> dict:
        # Backends flush their table stats when they exit, a moment after the
        # side process: wait until two reads agree on a count above zero
        # (zero only if it stays zero for 5 s).
        last = None
        for attempt in range(20):
            time.sleep(0.25)
            stats = self.pg_admin("stats", schema)
            if stats == last and (any(stats.values()) or attempt >= 19):
                break
            last = stats
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
        elif case == "compaction":
            job.update(COMPACTION, fixture=str(FIXTURES / "compaction.json"))
        elif case == "tools":
            name, calls = TOOL_FIXTURES[variant]
            if side == "py" and variant == "Glob":
                name = "tools_glob_py.json"
            job.update(fixture=str(FIXTURES / name), calls=calls)
            job.update(self.workspace(side, variant, with_tree=variant == "Grep-2k"))
        elif case == "mcp":
            if not Path(self.args.mcp_server_bin).is_file():
                return f"MCP server not built: {self.args.mcp_server_bin}"
            job.update(
                fixture=str(FIXTURES / "mcp_echo.json"),
                calls=10,
                mcp_server_bin=self.args.mcp_server_bin,
            )
        elif case == "decode":
            stream = self.sse_stream(variant)
            job.update(stream, vendor=variant, system=SYSTEM)
        elif case == "worker":
            job.update(fixture=str(FIXTURES / "text.json"), jobs=5 if self.args.quick else 100)
        elif case == "workflow":
            job.update(steps=10 if self.args.quick else 100, stores=variant)
        elif case == "evals":
            job["fixture"] = str(FIXTURES / "evals.json")
        elif case == "concurrency":
            job.update(fixture=str(FIXTURES / "loop_10x1.json"), sessions=CONCURRENT_SESSIONS)
        elif case == "sessions":
            if not self.args.pg_dsn:
                return "no --pg-dsn given"
            name = f"{side}__sessions__{variant}"
            root = self.out / "work" / name
            root.mkdir(parents=True, exist_ok=True)
            raised = variant.endswith("+caps")
            job.update(
                sessions=int(variant.split("x")[0].split("+")[0]),
                sidecars=self.args.sidecars if "x" in variant else 1,
                raise_caps=raised,
                pg_pool=RAISED_PG_POOL if raised else None,
                workspace=str(root),
                stores="postgres",
                base_url=self.chat_server(name),
            )
        elif case == "live":
            if not self.args.live:
                return "live run off; pass --live"
            if "DEEPSEEK_API_KEY" not in os.environ:
                return "DEEPSEEK_API_KEY not set"
            job.update(LIVE, iterations=len(LIVE["prompts"]), warmup=0)
        else:
            return f"unknown case {case}"
        return job

    def workspace(self, side: str, variant: str, with_tree: bool) -> dict:
        """A fresh copy of the tool workspace for one cell."""
        template = self.out / "work" / "template"
        if not template.exists():
            build_workspace(template)
        root = self.out / "work" / f"{side}__tools__{variant}"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        shutil.copytree(template / "src", root / "src")
        if with_tree:
            shutil.copytree(template / "tree", root / "tree")
        return {"workspace": str(root), "pristine": str(template / "src")}

    def sse_stream(self, vendor: str) -> dict:
        """Start the loopback SSE server once; this vendor's stream details."""
        dump = self.out / "sse"
        if self.sse is None:
            deltas = 2_000 if self.args.quick else 20_000
            self.sse = subprocess.Popen(
                [sys.executable, "-I", str(HERE / "sse_server.py"), "--deltas", str(deltas)]
                + ["--dump", str(dump)],
                stdout=subprocess.PIPE,
                text=True,
                env=scrubbed_env(),
            )
            assert self.sse.stdout is not None
            line = self.sse.stdout.readline()
            if not line.startswith("PORT "):
                raise RuntimeError("the SSE server did not start")
            self.sse_url = f"http://127.0.0.1:{int(line.split()[1])}"
        info = json.loads((dump / "streams.json").read_text())[vendor]
        suffix = {"openai-chat": "/v1", "openai-responses": "/v1"}.get(vendor, "")
        return {
            "base_url": f"{self.sse_url}/{vendor}{suffix}",
            "gemini_base_url": f"{self.sse_url}/gemini",
            "model": info["model"],
            "stream_bytes": info["bytes"],
            "stream_events": info["events"],
            "stream_sha256": info["sha256"],
            "expect_chars": info["text_chars"],
        }

    def chat_server(self, name: str) -> str:
        """Start this cell's chat server (case 14); its base URL."""
        if self.chat is not None:  # a cell that failed before its own stop
            self.stop_chat()
        self.chat_stats = self.out / "chat" / f"{name}.jsonl"
        self.chat_stats.parent.mkdir(exist_ok=True)
        self.chat_stats.unlink(missing_ok=True)  # the workers append to it
        command = [
            sys.executable,
            "-I",
            str(HERE / "chat_server.py"),
            "--stats",
            str(self.chat_stats),
        ]
        command += ["--delay", str(self.args.turn_delay), "--workers", str(self.args.chat_workers)]
        self.chat = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=scrubbed_env())
        line = self.chat.stdout.readline() if self.chat.stdout else ""
        if not line.startswith("PORT "):
            raise RuntimeError("the chat server did not start")
        return f"http://127.0.0.1:{int(line.split()[1])}/v1"

    def stop_chat(self) -> dict:
        """Stop the cell's chat server; its request count and own overhead per request."""
        chat, self.chat = self.chat, None
        assert chat is not None
        chat.terminate()
        chat.wait(timeout=30)
        lines = self.chat_stats.read_text().splitlines() if self.chat_stats.is_file() else []
        ms = [ms for line in lines for ms in json.loads(line)["overhead_ms"]]
        late = {"overhead_p50_ms": pct(ms, 50), "overhead_p99_ms": pct(ms, 99)} if ms else {}
        return {"requests": len(ms), **late, "overhead_max_ms": max(ms, default=0.0)}

    def sampled(self, command: list[str], stderr, tree: dict) -> subprocess.CompletedProcess:
        """Run a case 14 side through its two marks. At each, read the process
        tree's CPU (and at the start the Postgres commits), then let the side
        go on; in between, sample the tree's peaks. Fills `tree`."""
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            env=self.env_for("sessions"),
            cwd=self.out / "work",
        )
        killed = threading.Event()
        timer = threading.Timer(self.args.timeout, lambda: (killed.set(), proc.kill()))
        timer.start()
        sampler = Sampler(proc.pid)
        lines = []
        assert proc.stdout is not None and proc.stdin is not None
        try:
            for line in proc.stdout:
                if not line.startswith('{"mark"'):
                    lines.append(line)
                    continue
                if json.loads(line)["mark"] == "start":
                    tree["pg_commits_start"] = self.pg_admin("commits", "")
                    sampler.begin()
                else:
                    tree.update(sampler.end())
                proc.stdin.write("go\n")
                proc.stdin.flush()
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if sampler.is_alive():
                sampler.end()
        if killed.is_set():
            raise subprocess.TimeoutExpired(command, self.args.timeout)
        return subprocess.CompletedProcess(command, proc.returncode, "".join(lines))

    def env_for(self, case: str) -> dict[str, str]:
        env = scrubbed_env()
        if case == "live" and "DEEPSEEK_API_KEY" in os.environ:
            env["DEEPSEEK_API_KEY"] = os.environ["DEEPSEEK_API_KEY"]
        if case in ("decode", "sessions"):
            # The loopback servers ignore keys; the adapters only need one set.
            for key in DUMMY_KEYS:
                env[key] = "bench-dummy-key"
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
        tree: dict = {}
        try:
            with log.open("w") as stderr:
                if case == "sessions":
                    proc = self.sampled(self.command(side, job_path), stderr, tree)
                else:
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
            if self.chat is not None:
                result["provider"] = self.stop_chat()
            if schema is not None:
                try:
                    if "pg_commits_start" in tree:  # before the tuple reads, which commit
                        result["pg_commits"] = self.pg_commits(tree.pop("pg_commits_start"))
                    result["pg_tuple_writes"] = self.pg_writes(schema)
                finally:
                    self.pg_admin("drop", schema)
        result.update(tree)
        result["process_wall_s"] = time.perf_counter() - started
        return result

    def run_startup(self, side: str) -> dict:
        """Case 10: time whole cold processes (spawn to reaped), one per iteration.

        CPU and the fallback RSS come from `wait4`, so they include the
        rs-client sidecar; the RSS shown is what the process reported
        (rs-client: client plus sidecar).
        """
        reason = self.unavailable(side)
        if reason is not None:
            return {"status": "n/a", "reason": reason}
        job = self.base_job("startup", "cold")
        job["fixture"] = str(FIXTURES / "text.json")
        name = f"{side}__startup__cold"
        job_path = self.out / "jobs" / f"{name}.json"
        job_path.write_text(json.dumps(job, indent=2))
        walls: list[float] = []
        cpu = rss = 0.0
        reported: dict = {}
        with (self.out / "logs" / f"{name}.log").open("w") as stderr:
            for i in range(self.warmup + self.iterations):
                started = time.perf_counter()
                proc = subprocess.Popen(
                    self.command(side, job_path),
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    text=True,
                    env=self.env_for("startup"),
                    cwd=self.out / "work",
                )
                assert proc.stdout is not None
                out = proc.stdout.read()
                proc.stdout.close()
                _, status, usage = os.wait4(proc.pid, 0)
                proc.returncode = os.waitstatus_to_exitcode(status)
                wall = 1000 * (time.perf_counter() - started)
                lines = [line for line in out.splitlines() if line.startswith("{")]
                reported = json.loads(lines[-1]) if lines else {}
                if reported.get("status") != "ok":
                    return reported or {"status": "error", "error": f"exit {proc.returncode}"}
                if i >= self.warmup:
                    walls.append(wall)
                    cpu += 1000 * (usage.ru_utime + usage.ru_stime)
                    rss = max(rss, reported.get("peak_rss_mb") or usage.ru_maxrss / 1024)
        phases = reported.get("phases_ms", {})
        note = ", ".join(f"{k} {v:.1f}" for k, v in phases.items())
        return {
            "status": "ok",
            "iterations": len(walls),
            "wall_ms": walls,
            "cpu_ms": cpu,
            "ops_per_iter": 1,
            "op": "process",
            "peak_rss_mb": rss,
            "phases_ms": phases,
            "note": f"last process: {note} ms" if note else "",
        }

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
                for variant in variants_of(case, variants, self.args):
                    for side in SIDES:
                        key = f"{case}/{variant}"
                        spread = case == "sessions" and "x" in variant  # N sidecars
                        if side not in sides or (spread and side != "rs-client"):
                            continue
                        if case == "rpc_overhead":
                            result = (
                                self.derive_rpc(variant) if side == "rs-client" else NO_RPC[side]
                            )
                        else:
                            print(f"[{side}] {key} ...", file=sys.stderr, flush=True)
                            try:
                                if case == "startup":
                                    result = self.run_startup(side)
                                else:
                                    result = self.run_cell(side, case, variant)
                            except Exception as error:  # noqa: BLE001 - one cell, not the run
                                result = {"status": "error", "error": f"harness: {error}"}
                            print(f"[{side}] {key}: {summary(result)}", file=sys.stderr, flush=True)
                        self.results[side][key] = result
        finally:
            for server in (self.sse, self.chat):
                if server is not None:
                    server.terminate()
                    server.wait()
        self.write(sides)

    def py_linch(self) -> dict:
        """Where the py side's `linch` comes from, and that checkout's commit."""
        probe = "import json, linch; print(json.dumps([linch.__file__, linch.__version__]))"
        try:
            proc = subprocess.run(
                [self.args.py_python, "-I", "-c", probe],
                capture_output=True,
                text=True,
                env=scrubbed_env(),
                timeout=60,
            )
            path, version = json.loads(proc.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {}
        return {"file": path, "version": version, "commit": git_head(Path(path).parent)}

    def provenance(self) -> dict:
        return {
            "quick": self.args.quick,
            "iterations": self.iterations,
            "warmup": self.warmup,
            "bench_commit": git_head(HERE),
            "py_linch": self.py_linch(),
            "py_python": self.args.py_python,
            "client_python": self.args.client_python,
            "server_bin": self.args.server_bin,
            "kernel_bin": self.args.kernel_bin,
            "turn_delay": self.args.turn_delay,
            "chat_workers": self.args.chat_workers,
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
    if "run_wall_ms" in result:
        runs, window = len(result["run_wall_ms"]), result["window_ms"]
        return f"{runs} runs in {window:.0f} ms, {result['failed']} failed {result['errors']}"
    return f"{per_op(result)}, wall {statistics.median(result['wall_ms']):.3f} ms"


def per_op(result: dict) -> str:
    wall = statistics.median(result["wall_ms"])
    ops, op = result["ops_per_iter"], result["op"]
    if op == "event":
        mb = result.get("bytes_per_iter", 0) / 1e6
        text = f"{mb / (wall / 1000):.1f} MB/s, {ops / (wall / 1000) / 1000:.1f} k ev/s"
    elif op in ("job", "run"):
        text = f"{ops / (wall / 1000):.1f} {op}s/s"
    else:
        text = f"{wall / ops:.3f} ms/{op}"
    note = result.get("note")
    return f"{text} ({note})" if note else text


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


def sessions_table(results: dict, sides: list[str], args: argparse.Namespace) -> list[str]:
    """Case 14's own table: its columns are not the main table's."""
    delay, turns = args.turn_delay, SESSION_TURNS
    lines = [
        "",
        "## 14. Concurrent sessions, Postgres",
        "",
        f"Provider delay {delay:g} s per turn; overhead per turn is (run wall - {turns} x delay) "
        f"/ {turns}. CPU, RSS, children and fds: the side's process tree (bench/README.md).",
        "",
        "| M | Side | Run wall p50 / p99 s | Overhead p50 / p99 ms/turn | Turns/s | PG commits/s "
        "| CPU s | Peak RSS MB | Peak children | Peak fds | Provider p99 ms | Failed |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for variant in variants_of("sessions", (), args):
        for side in sides:
            result = results[side].get(f"sessions/{variant}")
            if result is None:
                continue
            m = variant.split("x")[0].split("+")[0]
            label = side + (f", {args.sidecars} sidecars" if "x" in variant else "")
            label += ", caps raised" if variant.endswith("+caps") else ""
            if result.get("status") != "ok":
                why = str(result.get("reason") or result.get("error") or "?")
                why = why.replace("|", "/").replace("\n", " ")
                lines.append(f"| {m} | {label} | {result.get('status')} ({why}) |" + " |" * 9)
                continue
            walls = result["run_wall_ms"]
            window = result["window_ms"] / 1000
            cells = ["-", "-"]
            if walls:
                over = [(wall - turns * delay * 1000) / turns for wall in walls]
                cells = [
                    f"{pct(walls, 50) / 1000:.2f} / {pct(walls, 99) / 1000:.2f}",
                    f"{pct(over, 50):.1f} / {pct(over, 99):.1f}",
                ]
            commits = result.get("pg_commits")
            provider = result.get("provider", {}).get("overhead_p99_ms")
            failed = str(result["failed"])
            if result["failed"]:
                failed += " (" + "; ".join(result["errors"]).replace("|", "/") + ")"
            cells += [
                f"{len(walls) * turns / window:.0f}",
                "n/c" if commits is None else f"{commits / window:.0f}",
                f"{result.get('cpu_s', 0):.1f}",
                f"{result.get('peak_rss_mb', 0):.0f}",
                str(result.get("peak_children", "n/c")),
                ("<= " if result.get("fds_bound") else "") + str(result.get("peak_fds", "n/c")),
                "n/c" if provider is None else f"{provider:.1f}",
                failed,
            ]
            lines.append(f"| {m} | {label} | {' | '.join(cells)} |")
    return lines


def table(results: dict, sides: list[str], prov: dict, args: argparse.Namespace) -> str:
    lines = [
        "# Python Linch vs linch-rust",
        "",
        "Smoke run (`--quick`): checks the harness only; these are not results."
        if args.quick
        else f"{prov['iterations']} timed iterations after {prov['warmup']} warm-up per cell.",
        "",
        f"Bench (linch-rust) commit {prov['bench_commit']}; py side Linch "
        f"{prov['py_linch'].get('version')} at {prov['py_linch'].get('commit')}.",
        f"{prov['date']}, {prov['cpus']} CPUs, "
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
        if case == "sessions":
            lines += sessions_table(results, sides, args)
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
    parser.add_argument(
        "--sessions", default="", help="case 14 session counts (100,500,1000,2000; --quick 20,50)"
    )
    parser.add_argument("--sidecars", type=int, default=4, help="case 14 rs-client spread rows")
    parser.add_argument(
        "--turn-delay", type=float, default=1.0, help="case 14 provider delay per turn (s)"
    )
    parser.add_argument("--chat-workers", type=int, default=16, help="case 14 server processes")
    args = parser.parse_args()
    args.sessions = tuple(int(m) for m in args.sessions.split(",") if m.strip())
    args.cases = {c.strip() for c in args.cases.split(",") if c.strip()}
    args.sides = {s.strip() for s in args.sides.split(",") if s.strip()}
    args.client_python = args.client_python or args.py_python
    # Side processes run in DIR/work, so every path they get is absolute.
    for name in ("server_bin", "kernel_bin", "mcp_server_bin"):
        setattr(args, name, str(Path(getattr(args, name)).resolve()))
    unknown = args.sides - set(SIDES)
    if unknown:
        parser.error(f"unknown sides: {', '.join(sorted(unknown))}")
    work = Path(args.out).resolve() / "work"
    if work.exists():
        shutil.rmtree(work)
    Runner(args).run()


if __name__ == "__main__":
    main()

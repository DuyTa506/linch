#!/usr/bin/env python3
"""Bounded multiprocess worker experiments; run under flock /tmp/wp8-cpu.lock."""

import argparse
import asyncio
import contextlib
import json
import os
import random
import signal
import socket
import sys
import tempfile
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import asyncpg
import psutil  # pyright: ignore[reportMissingModuleSource]

from linch.providers.base import BaseProvider

TERMINAL = "('completed','failed','aborted')"


async def pool_for(a: argparse.Namespace, *, direct: bool = False, name: str = "audit") -> Any:
    return await asyncpg.create_pool(
        a.dsn if direct else a.proxy_dsn,
        min_size=2,
        max_size=a.pool_size,
        command_timeout=10,
        server_settings={"search_path": a.schema, "application_name": name},
    )


class TimedProvider(BaseProvider):
    id = "load-test"

    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req: Any):
        from linch.types import Usage

        turn = sum(getattr(m, "role", None) == "assistant" for m in req.messages)
        yield {"type": "message_start", "model": req.model}
        await asyncio.sleep(self.a.ttft)
        names = ["RAG", "SQL", "save_note"]
        value = json.dumps({"value": "load test"}) if turn < 3 else "completed load test"
        if turn < 3:
            yield {"type": "tool_use_start", "id": f"tool_{turn}", "name": names[turn]}
        for i in range(20):
            await asyncio.sleep(self.a.delta_window / 20)
            part = value[len(value) * i // 20 : len(value) * (i + 1) // 20]
            if turn < 3:
                yield dict(type="tool_use_input_delta", id=f"tool_{turn}", json_delta=part)
            else:
                yield dict(type="text_delta", text=part)
        if turn < 3:
            yield {"type": "tool_use_end", "id": f"tool_{turn}"}
        end = dict(type="message_end", usage=Usage(input_tokens=120, output_tokens=20))
        end["stop_reason"] = "tool_use" if turn < 3 else "end_turn"
        yield end


def tools(pool: Any, a: argparse.Namespace) -> Any:
    from linch import ToolContext, ToolResult, empty_tools, tool

    def read_tool(name: str) -> Any:
        async def read(value: str, ctx: ToolContext) -> ToolResult:
            await asyncio.sleep(random.uniform(a.tool_low, a.tool_high))
            return ToolResult(content=value)

        return tool(read, name=name, scope="read")

    async def save_note(value: str, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(random.uniform(a.tool_low, a.tool_high))
        run_id, tool_id = ctx.idempotency_key.rsplit(":", 1)
        query = """INSERT INTO load_notes VALUES($1,$2,$3,1)
            ON CONFLICT(run_id,tool_id) DO UPDATE SET attempts=load_notes.attempts+1"""
        await pool.execute(query, run_id, tool_id, value)
        return ToolResult(content="saved")

    save: Any = tool(save_note, name="save_note", scope="write")
    return empty_tools(read_tool("RAG"), read_tool("SQL"), save)


AUDIT_DDL = """
CREATE TABLE load_notes(run_id text,tool_id text,note text,attempts int,
 PRIMARY KEY(run_id,tool_id));
CREATE TABLE load_claims(run_id text,owner text,epoch bigint,
 at timestamptz DEFAULT clock_timestamp());
CREATE TABLE load_writes(run_id text,seq bigint,actor text,owner text);
CREATE FUNCTION load_claim() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.lease_owner IS NOT NULL AND (OLD.lease_owner IS DISTINCT FROM NEW.lease_owner
 OR OLD.lease_epoch IS DISTINCT FROM NEW.lease_epoch) THEN
 INSERT INTO load_claims(run_id,owner,epoch) VALUES(NEW.id,NEW.lease_owner,NEW.lease_epoch);
 END IF; RETURN NEW; END $$;
CREATE TRIGGER load_claim AFTER UPDATE OF lease_owner,lease_epoch ON runs
 FOR EACH ROW EXECUTE FUNCTION load_claim();
CREATE FUNCTION load_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 INSERT INTO load_writes SELECT NEW.run_id,NEW.seq,current_setting('application_name'),
 lease_owner FROM runs WHERE id=NEW.run_id
 AND current_setting('application_name') IS DISTINCT FROM lease_owner; RETURN NEW; END $$;
CREATE TRIGGER load_write AFTER INSERT ON run_events FOR EACH ROW EXECUTE FUNCTION load_write();
"""


async def child(a: argparse.Namespace) -> None:
    from linch import Agent, PostgresRunStore
    from linch.sessions.postgres import PostgresSessionStore
    from linch.worker import Worker, enqueue_run

    pool = await pool_for(a, name=a.identity)
    stop = asyncio.Event()
    metrics: dict[str, Any] = defaultdict(int, errors=Counter(), lag=[], pool=[], renew_failures=0)
    path = Path(a.logs) / f"{a.identity}.json"

    def save() -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(metrics))
        temporary.replace(path)

    async def monitor() -> None:
        while not stop.is_set():
            expected = time.monotonic() + 0.1
            await asyncio.sleep(0.1)
            if a.steady <= time.time() < a.end:
                metrics["lag"].append(max(0, time.monotonic() - expected) * 1000)
                metrics["pool"].append(pool.get_size() - pool.get_idle_size())
            save()

    async def factory(job: Any) -> Agent:
        return Agent(
            model="load-test",
            provider=TimedProvider(a),
            tools=tools(job.pool, a),
            permissions={"mode": "skip-dangerous"},
            run_store=job.run_store,
            session_store=PostgresSessionStore("", pool=job.pool),
        )

    worker = None
    if a.identity != "api":
        original = PostgresRunStore.renew

        async def renew(self: Any, lease: Any) -> Any:
            try:
                return await original(self, lease)
            except Exception:
                metrics["renew_failures"] += 1
                raise

        PostgresRunStore.renew = renew
        worker = Worker(
            pool,
            factory,
            worker_id=a.identity,
            queues={"default": a.concurrency},
            lease_ttl_s=a.lease_ttl,
            stall_timeout_s=a.stall_timeout,
            poll_interval_s=a.poll_interval,
        )

    def stopping() -> None:
        if worker:
            worker.stop()
        else:
            stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stopping)
    monitoring = asyncio.create_task(monitor())
    readers = []
    try:
        save()
        if worker:
            await worker.run()
        else:
            metrics["reader_results"] = [0] * a.readers
            readers = [
                asyncio.create_task(reader(pool, stop, metrics, i)) for i in range(a.readers)
            ]
            pending: set[asyncio.Task[Any]] = set()

            async def submit() -> None:
                try:
                    await enqueue_run(pool, uuid4().hex, "hello", meta={"arrival": time.time()})
                except Exception as exc:
                    metrics["errors"][type(exc).__name__] += 1

            offset = time.time() - time.monotonic()
            next_at = a.start - offset
            while next_at < a.end - offset and not stop.is_set():
                await asyncio.sleep(max(0, next_at - time.monotonic()))
                task = asyncio.create_task(submit())
                pending.add(task)
                task.add_done_callback(pending.discard)
                next_at += random.expovariate(a.rate)
            await asyncio.gather(*pending)
            metrics["enqueued"] = True
            save()
            await stop.wait()
    finally:
        stop.set()
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await monitoring
        save()
        await asyncio.wait_for(pool.close(), 10)


async def reader(pool: Any, stop: asyncio.Event, metrics: dict[str, Any], index: int) -> None:
    rid, cursor, complete = None, 0, True
    while not stop.is_set():
        try:
            if complete:
                latest = await pool.fetchval("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1")
                if latest != rid:
                    rid, cursor, complete = latest, 0, False
            query = """SELECT seq,extract(epoch FROM appended_at::timestamptz)::float at,
                event::jsonb->'event'->>'type' kind FROM run_events
                WHERE run_id=$1 AND seq>$2 ORDER BY seq"""
            rows = await pool.fetch(query, rid, cursor)
            for row in rows:
                metrics["reader_seq_errors"] += int(row["seq"] != cursor + 1)
                cursor = row["seq"]
                metrics.setdefault("read_lag", []).append((time.time() - row["at"]) * 1000)
            if rows and rows[-1]["kind"] == "result":
                metrics["reader_results"][index] += 1
                complete = True
        except (asyncpg.PostgresError, OSError, TimeoutError, asyncpg.InterfaceError):
            metrics["reader_retries"] += 1
        await asyncio.sleep(0.1)


async def http(port: int, path: str, payload: Any = None) -> Any:
    def request() -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.load(response)

    return await asyncio.to_thread(request)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def stop_process(proc: Any, timeout: float) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except TimeoutError:
        proc.kill()
        await asyncio.wait_for(proc.wait(), 10)


async def pg_stats(pool: Any) -> Any:
    await pool.execute("SELECT pg_stat_clear_snapshot()")
    row = await pool.fetchrow("""SELECT xact_commit::float commits,tup_inserted::float rows,
        (SELECT wal_bytes::float FROM pg_stat_wal) wal
        FROM pg_stat_database WHERE datname=current_database()""")
    return dict(row), time.monotonic()


async def summarize(pool: Any, a: argparse.Namespace) -> dict[str, Any]:
    query = f"""WITH e AS (SELECT run_id,count(*) n,min(seq) lo,max(seq) hi,
      count(*) FILTER(WHERE event::jsonb->'event'->>'type'='result') results,
      max(seq) FILTER(WHERE event::jsonb->'event'->>'type'='result') result_seq,
   count(*) FILTER(WHERE event::jsonb->'event'->>'type'='error') errors FROM run_events GROUP BY 1
    ), claims AS (SELECT run_id,min(at) first_claim FROM load_claims GROUP BY 1),
    cohort AS (SELECT r.*,extract(epoch FROM updated_at::timestamptz) finish,
      (meta::jsonb->>'arrival')::float arrival,first_claim,e.* FROM runs r
      LEFT JOIN e ON e.run_id=r.id LEFT JOIN claims ON claims.run_id=r.id)
    SELECT count(*)::int runs,count(*) FILTER(WHERE status IN {TERMINAL})::int terminal,
      count(*) FILTER(WHERE status='completed')::int completed,
      count(*) FILTER(WHERE status='failed')::int failed,
      json_build_object('terminal_count',
        count(*) FILTER(WHERE status<>ALL($4::text[]) OR coalesce(results,0)<>1)
        +CASE WHEN count(*)=0 THEN 1 ELSE 0 END,
        'seq_gaps',count(*) FILTER(WHERE lo IS DISTINCT FROM 1 OR hi<>n),
        'after_result',count(*) FILTER(WHERE result_seq<>hi),
        'failed_without_error',count(*) FILTER(WHERE status='failed' AND coalesce(errors,0)=0),
        'orphans',count(*) FILTER(WHERE status NOT IN {TERMINAL} AND NOT EXISTS
          (SELECT 1 FROM pgqueuer q WHERE q.dedupe_key='run:'||cohort.id)),
        'writer_mismatch',(SELECT count(*) FROM load_writes),
        'duplicate_notes',(SELECT count(*) FROM (SELECT run_id,tool_id FROM load_notes
            GROUP BY 1,2 HAVING count(*)>1)s)) invariant_errors,
      (SELECT coalesce(sum(attempts-1),0)::bigint FROM load_notes) note_conflicts,
      count(*) FILTER(WHERE status='completed' AND finish >= $1 AND finish < $2)::float/$3 tps,
      percentile_cont(ARRAY[.5,.95,.99]) WITHIN GROUP(ORDER BY (finish-arrival)*1000)
        FILTER(WHERE arrival >= $1 AND arrival < $2 AND status IN {TERMINAL}) latency,
      percentile_cont(ARRAY[.5,.99]) WITHIN GROUP(ORDER BY
        (extract(epoch FROM first_claim)-arrival)*1000)
        FILTER(WHERE arrival >= $1 AND arrival < $2) wait,
      (SELECT count(*) FROM run_events WHERE event::jsonb->'event'->>'type'='assistant'
        AND appended_at::timestamptz>=to_timestamp($1)
        AND appended_at::timestamptz<to_timestamp($2)) turns,
      (SELECT json_object_agg(kind,n) FROM (SELECT coalesce(
        event::jsonb->'event'->'error'->>'name',event::jsonb->'event'->'error'->>'type',
        'unknown') kind,count(*) n FROM run_events
        WHERE event::jsonb->'event'->>'type'='error' GROUP BY 1)errors) errors_by_type
    FROM cohort"""
    accepted = ["completed", "failed"] if a.chaos == "blip" else ["completed"]
    row = await pool.fetchrow(query, a.steady, a.end, a.duration, accepted)
    result = dict(row)
    for key in ("invariant_errors", "errors_by_type"):
        result[key] = json.loads(result[key] or "{}")
    for source, target in (("latency", "latency_ms"), ("wait", "wait_ms")):
        values = result.pop(source)
        names = ("p50", "p95", "p99") if source == "latency" else ("p50", "p99")
        result[target] = dict(zip(names, values or [None] * len(names), strict=True))
    return result


async def run_point(a: argparse.Namespace) -> dict[str, Any]:
    from linch.worker import install

    a = argparse.Namespace(**vars(a))
    a.schema = "load_" + uuid4().hex
    a.logs = tempfile.mkdtemp(prefix="linch-load-")
    processes: dict[str, Any] = {}
    handles = []
    admin = await asyncpg.connect(a.dsn, command_timeout=10)
    pool = None

    async def spawn(identity: str, command: list[str]) -> None:
        handle = open(Path(a.logs) / f"{identity}.stderr", "wb")
        handles.append(handle)
        process = await asyncio.create_subprocess_exec(*command, stdout=handle, stderr=handle)
        processes[identity] = process

    try:
        await admin.execute(f'CREATE SCHEMA "{a.schema}"')
        a.proxy_dsn = a.dsn
        if not a.direct:
            api, port = free_port(), free_port()
            await spawn("proxy", ["toxiproxy-server", "-host", "127.0.0.1", "-port", str(api)])
            for _ in range(50):
                try:
                    await http(api, "/version")
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            proxy = dict(name="pg", listen=f"127.0.0.1:{port}", upstream="127.0.0.1:55432")
            await http(api, "/proxies", proxy)
            for direction in ("upstream", "downstream"):
                toxic: dict[str, Any] = dict(name=direction, type="latency", stream=direction)
                delay = int(a.rtt) if direction == "upstream" else 0
                toxic["attributes"] = dict(latency=delay, jitter=0)
                await http(api, "/proxies/pg/toxics", toxic)
            parsed = urlsplit(a.dsn)
            address = f"{parsed.username or 'postgres'}@127.0.0.1:{port}"
            a.proxy_dsn = parsed._replace(netloc=address).geturl()
        pool = await pool_for(a, direct=True)
        await install(pool)
        await pool.execute(AUDIT_DDL)
        a.start = time.time() + 2
        a.steady = a.start + a.warmup
        a.end = a.steady + a.duration
        config = Path(a.logs) / "config.json"
        config.write_text(json.dumps(vars(a)))
        for identity in [*(f"w-{i}" for i in range(a.workers)), "api"]:
            command = [sys.executable, __file__, "__child__", str(config), identity]
            await spawn(identity, command)
        root = psutil.Process(await pool.fetchval("SELECT pg_backend_pid()")).parent()
        previous: dict[int, float] = {}
        cpu: dict[str, float] = defaultdict(float)
        waits: Counter[str] = Counter()
        connections = []
        before = after = {}
        sample_start = sample_end = time.monotonic()
        chaos: dict[str, Any] = {"mode": a.chaos}
        done_chaos = False
        deadline = a.end + a.drain_timeout
        while time.time() < deadline:
            now = time.time()
            if now >= a.steady and not before:
                before, sample_start = await pg_stats(pool)
            if a.steady <= now < a.end:
                tracked = [
                    (name, psutil.Process(p.pid))
                    for name, p in processes.items()
                    if p.returncode is None and name != "api"
                ]
                if root:
                    tracked += [("postgres", p) for p in [root, *root.children(recursive=True)]]
                for name, proc in tracked:
                    with contextlib.suppress(psutil.Error):
                        current = sum(proc.cpu_times()[:2])
                        cpu[name] += max(0, current - previous.get(proc.pid, current))
                        previous[proc.pid] = current
                query = """SELECT coalesce(wait_event_type,'CPU')||':'||
                    coalesce(wait_event,'running') key,count(*) n FROM pg_stat_activity
                    WHERE datname=current_database() GROUP BY 1"""
                activity = await pool.fetch(query)
                waits.update({r["key"]: r["n"] for r in activity})
                connections.append(sum(r["n"] for r in activity))
                if a.chaos != "none" and not done_chaos and now >= a.steady + a.duration / 3:
                    owner = await pool.fetchrow(
                        "SELECT id,lease_owner,lease_epoch FROM runs "
                        "WHERE status='running' AND lease_owner IS NOT NULL LIMIT 1"
                    )
                    if owner:
                        chaos.update(dict(owner), injected_at=time.time())
                        if a.chaos == "blip":
                            await http(api, "/proxies/pg", {"enabled": False})
                            await asyncio.sleep(3)
                            await http(api, "/proxies/pg", {"enabled": True})
                        elif a.chaos == "kill":
                            processes[owner["lease_owner"]].kill()
                            await asyncio.wait_for(processes[owner["lease_owner"]].wait(), 5)
                        else:
                            processes[owner["lease_owner"]].terminate()
                        done_chaos = True
            if now >= a.end:
                if not after:
                    after, sample_end = await pg_stats(pool)
                api_path = Path(a.logs) / "api.json"
                enqueued = api_path.exists() and json.loads(api_path.read_text()).get("enqueued")
                remaining = await pool.fetchval(
                    f"SELECT count(*) FROM runs WHERE status NOT IN {TERMINAL}"
                )
                if enqueued and not remaining:
                    break
            await asyncio.sleep(0.25)
        children = [p for name, p in processes.items() if name != "proxy"]
        await asyncio.gather(*(stop_process(p, a.drain_timeout) for p in children))
        result = await summarize(pool, a)
        if done_chaos:
            chaos["claims_after_signal"] = await pool.fetchval(
                "SELECT count(*) FROM load_claims WHERE owner=$1 AND extract(epoch FROM at)>$2",
                chaos["lease_owner"],
                chaos["injected_at"],
            )
            query = """SELECT epoch,at::text FROM load_claims WHERE run_id=$1
                AND epoch>$2 ORDER BY at LIMIT 1"""
            recovery = await pool.fetchrow(query, chaos["id"], chaos["lease_epoch"])
            chaos["recovery"] = dict(recovery) if recovery else None
        result.update({key: getattr(a, key) for key in ("workers", "rate", "rtt", "logs")})
        checks = result["invariant_errors"]
        ignored = ("proxy", chaos.get("lease_owner") if a.chaos == "kill" else None)
        codes = [p.returncode for n, p in processes.items() if n not in ignored]
        checks["processes"] = sum(code != 0 for code in codes)
        checks["chaos"] = int(a.chaos != "none" and not done_chaos)
        checks["recovery"] = int(a.chaos == "kill" and not chaos.get("recovery"))
        checks["drain_claims"] = chaos.get("claims_after_signal", 0) if a.chaos == "drain" else 0
        result["chaos"] = chaos
        result["cpu_pct"] = {k: v / a.duration * 100 for k, v in cpu.items()}
        result["pg_wait_samples"] = dict(waits)
        result["connections_max"] = max(connections, default=0)
        elapsed = sample_end - sample_start
        result["pg"] = {f"{k}_per_s": (after[k] - before[k]) / elapsed for k in before}
        result["turns_per_s"] = result["turns"] / a.duration
        result["pg"]["commits_per_turn"] = (after["commits"] - before["commits"]) / max(
            result["turns"], 1
        )
        result["clients"] = {
            p.stem: json.loads(p.read_text())
            for p in Path(a.logs).glob("*.json")
            if p.stem not in {"config"}
        }
        for metrics in result["clients"].values():
            for key in ("lag", "pool", "read_lag"):
                if key in metrics:
                    query = """SELECT percentile_cont(.99) WITHIN GROUP(ORDER BY x) p99,
                        max(x) max FROM unnest($1::float[]) x"""
                    metrics[key] = dict(await pool.fetchrow(query, metrics[key]))
        api_metrics = result["clients"].get("api", {})
        checks["readers"] = int(a.readers > 0 and not all(api_metrics.get("reader_results", [0])))
        checks["reader_seq"] = api_metrics.get("reader_seq_errors", 0)
        print(f"M={a.workers}: {result['completed']}/{result['runs']} complete", file=sys.stderr)
        return result
    finally:
        await asyncio.gather(
            *(stop_process(p, 5) for p in processes.values()), return_exceptions=True
        )
        for handle in handles:
            handle.close()
        if pool:
            pool.terminate()
        try:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{a.schema}" CASCADE')
        finally:
            await asyncio.wait_for(admin.close(), 10)


async def experiments(a: argparse.Namespace) -> dict[str, Any]:
    points = []
    brackets = {}

    async def point(**changes: Any) -> dict[str, Any]:
        item = await run_point(argparse.Namespace(**(vars(a) | changes)))
        points.append(item)
        if a.output:
            Path(a.output).write_text(json.dumps(dict(points=points)))
        return item

    if a.quick:
        for mode in ("none", "kill", "blip", "drain"):
            await point(workers=4, rate=2, warmup=2, duration=15, chaos=mode, readers=100)
    elif a.command == "point":
        await point()
    elif a.command == "capacity":
        for m in (1, 2, 4, 8):
            rate, stable, base = a.rate, None, None
            for _ in range(6):
                result = await point(workers=m, rate=rate, rtt=1, readers=a.readers if base else 0)
                latency = result["latency_ms"]["p99"]
                base = base or latency
                bad = (
                    latency is None
                    or latency > 2 * base
                    or any(result["invariant_errors"].values())
                )
                if bad:
                    brackets[str(m)] = {"last_stable": stable, "first_bad": rate}
                    break
                stable, rate = rate, rate * 2
            else:
                brackets[str(m)] = {"last_stable": stable, "first_bad": None}
    elif a.command == "network":
        for rtt in (0, 1, 2):
            await point(workers=4, rtt=rtt, readers=0)
            if rtt == 1 and a.readers:
                await point(workers=4, rtt=rtt)
    else:
        for mode in ("kill", "blip", "drain"):
            await point(workers=4, rtt=1, chaos=mode)
    return dict(points=points, capacity_brackets=brackets)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "__child__":
        config = argparse.Namespace(**json.loads(Path(sys.argv[2]).read_text()))
        config.identity = sys.argv[3]
        asyncio.run(child(config))
        return
    p = argparse.ArgumentParser(description=__doc__)
    commands = ("point", "capacity", "network", "chaos")
    p.add_argument("command", nargs="?", choices=commands, default="point")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--pool-size", type=int, default=8)
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--readers", type=int, default=0)
    p.add_argument("--rate", type=float, default=1)
    p.add_argument("--rtt", type=int, default=1)
    p.add_argument("--warmup", type=float, default=15)
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--ttft", type=float, default=0.3)
    p.add_argument("--delta-window", type=float, default=0.7)
    p.add_argument("--tool-low", type=float, default=0.03)
    p.add_argument("--tool-high", type=float, default=0.08)
    p.add_argument("--lease-ttl", type=float, default=4)
    p.add_argument("--stall-timeout", type=float, default=6)
    p.add_argument("--poll-interval", type=float, default=0.2)
    p.add_argument("--drain-timeout", type=float, default=45)
    p.add_argument("--dsn", default="postgresql://postgres@127.0.0.1:55432/postgres")
    p.add_argument("--chaos", choices=("none", "kill", "blip", "drain"), default="none")
    p.add_argument("--direct", action="store_true")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--output")
    a = p.parse_args()
    if a.direct:
        a.dsn = os.environ.get("AGENT_KIT_TEST_PG_DSN", a.dsn)
    if a.rate <= 0 or a.duration <= 0 or a.workers < 1 or a.pool_size < 2:
        raise SystemExit("rate, duration, workers must be positive; pool-size >=2")
    if a.direct and a.chaos == "blip":
        raise SystemExit("blip requires Toxiproxy")
    report = asyncio.run(experiments(a))
    text = json.dumps(report, indent=2)
    if a.output:
        Path(a.output).write_text(text + "\n")
    else:
        print(text)
    raise SystemExit(int(any(any(p["invariant_errors"].values()) for p in report["points"])))


if __name__ == "__main__":
    main()

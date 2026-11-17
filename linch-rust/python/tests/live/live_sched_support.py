"""Helpers of the `test_live_sched_*.py` suites: real models, a real
`linch-server --config` with a worker, real Postgres.

Everything skips cleanly without the environment:

- `LINCH_LIVE_VLLM_URL` (for example `http://127.0.0.1:8201/v1`): the vLLM
  endpoint; the model is `LINCH_LIVE_VLLM_MODEL` or `qwen3.6-35b-a3b`.
- `DEEPSEEK_API_KEY`: read by the server from its environment; never printed.
- `LINCH_TEST_PG_DSN`: a Postgres database; each test gets its own schema.

The vLLM host is shared: one request at a time and small outputs, so the
tests that use it keep `max_output_tokens` low and run one model call at a
time unless a test says otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from linch_rust import (
    AgentConfig,
    Envelope,
    Job,
    ResultEvent,
    Run,
    RunLeased,
    Server,
    find_server,
)

VLLM_URL = os.environ.get("LINCH_LIVE_VLLM_URL", "")
VLLM_MODEL = os.environ.get("LINCH_LIVE_VLLM_MODEL", "qwen3.6-35b-a3b")
DEEPSEEK_MODEL = os.environ.get("LINCH_LIVE_DEEPSEEK_MODEL", "deepseek-flash")
HAS_DEEPSEEK = bool(os.environ.get("DEEPSEEK_API_KEY"))
PG_DSN = os.environ.get("LINCH_TEST_PG_DSN", "")

TOKEN = "tok-acme-live-sched-0001"
TOKEN_BETA = "tok-beta-live-sched-0001"

needs_pg = pytest.mark.skipif(not PG_DSN, reason="LINCH_TEST_PG_DSN is not set")
needs_vllm = pytest.mark.skipif(not VLLM_URL, reason="LINCH_LIVE_VLLM_URL is not set")
needs_deepseek = pytest.mark.skipif(not HAS_DEEPSEEK, reason="DEEPSEEK_API_KEY is not set")

KINDS = [
    pytest.param("vllm", marks=needs_vllm),
    pytest.param("deepseek", marks=needs_deepseek),
]


@pytest.fixture(params=KINDS)
def kind(request: pytest.FixtureRequest) -> str:
    """Each live provider in turn: `vllm`, `deepseek`."""
    return cast(str, request.param)


@pytest.fixture
def vllm() -> str:
    """vLLM only, for the tests too heavy to repeat on a paid provider."""
    if not VLLM_URL:
        pytest.skip("LINCH_LIVE_VLLM_URL is not set")
    return "vllm"


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    """A DSN whose `search_path` is a fresh schema, dropped at the end."""
    if not PG_DSN:
        pytest.skip("LINCH_TEST_PG_DSN is not set")
    import psycopg
    from psycopg import sql

    schema = f"ls_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            separator = "&" if "?" in PG_DSN else "?"
            yield f"{PG_DSN}{separator}options=-c%20search_path%3D{schema}"
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def query(dsn: str, statement: str, *args: Any) -> list[tuple[Any, ...]]:
    """Rows of a read-only `statement` against the test schema."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        return connection.execute(statement, args).fetchall()  # type: ignore[arg-type]


def model(kind: str) -> tuple[str, dict[str, Any]]:
    """`(spec.model, provider)` of a live provider."""
    if kind == "vllm":
        return VLLM_MODEL, {"id": "vllm", "base_url": VLLM_URL}
    return DEEPSEEK_MODEL, {"id": "deepseek"}


def live_config(
    kind: str,
    namespace: str,
    dsn: str | None,
    *,
    spec: dict[str, Any] | None = None,
    **extra: Any,
) -> AgentConfig:
    """An agent config on a live model (thinking off, small outputs), on
    Postgres when `dsn` is given. `spec` adds to or replaces `spec` fields."""
    name, provider = model(kind)
    fields: dict[str, Any] = {
        "namespace": namespace,
        "spec": {
            "model": name,
            "max_output_tokens": 600,
            "thinking": {"type": "disabled"},
            "system_prompt": "You are a terse test assistant. Follow instructions exactly.",
            **(spec or {}),
        },
        "provider": provider,
        **extra,
    }
    if dsn is not None:
        fields["stores"] = {"type": "postgres", "dsn": dsn, "lease_ttl_ms": 2000}
    return cast(AgentConfig, fields)


def tenant_allow(dsn: str, **allow: Any) -> dict[str, Any]:
    allowed: dict[str, Any] = {
        "postgres_dsns": [dsn],
        "permission_modes": ["default", "accept_edits", "skip_dangerous"],
        "max_schedules": 8,
        "max_queued_jobs": 32,
        "min_schedule_interval_ms": 1000,
        "max_runs": 16,
    }
    if VLLM_URL:
        allowed["provider_base_urls"] = [VLLM_URL]
    allowed.update(allow)
    return allowed


class LiveServer:
    """A `linch-server --config` process with a worker, killable and
    restartable on the same socket and config.

    Attributes:
        address: Where clients connect (`unix:...`).
        log: The server's stderr, as text, up to now.
    """

    def __init__(
        self,
        dsn: str,
        *,
        tenants: dict[str, dict[str, Any]] | None = None,
        worker: dict[str, Any] | None | bool = True,
        directory: Path | None = None,
    ) -> None:
        self.dsn = dsn
        self.directory = directory or Path(tempfile.mkdtemp(prefix="lv-", dir="/tmp"))
        self._owns_directory = directory is None
        config: dict[str, Any] = {
            "socket": str(self.directory / "linch.sock"),
            "tenants": tenants
            or {"acme": {"tokens": [{"value": TOKEN}], "allow": tenant_allow(dsn)}},
        }
        if worker is not False and worker is not None:
            fields = {"dsn": {"value": dsn}, "poll_interval_ms": 250, "retry_delay_ms": 500}
            if isinstance(worker, dict):
                fields.update(worker)
            config["worker"] = fields
        self.config = config
        self._path = self.directory / "server.json"
        self._path.write_text(json.dumps(config))
        self.process: asyncio.subprocess.Process | None = None
        self.address = ""
        self._log = self.directory / "server.log"

    @property
    def log(self) -> str:
        return self._log.read_text(errors="replace") if self._log.exists() else ""

    async def start(self) -> LiveServer:
        """Start the process and wait until it listens."""
        sink = self._log.open("ab")
        # Its stderr is a pipe only until the listening line, so a kill never
        # blocks on a reader; the rest goes to the log file.
        self.process = await asyncio.create_subprocess_exec(
            find_server(),
            "--config",
            str(self._path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.directory,
        )
        assert self.process.stderr is not None
        stderr = self.process.stderr
        while True:
            line = await asyncio.wait_for(stderr.readline(), 30)
            assert line, f"the server exited before it listened: {self.log}"
            sink.write(line)
            sink.flush()
            _, _, address = line.decode().strip().partition("listening on ")
            if address.startswith("unix:"):
                self.address = address
                break

        async def drain() -> None:
            while line := await stderr.readline():
                sink.write(line)
                sink.flush()
            sink.close()

        self._drain = asyncio.create_task(drain())
        return self

    @property
    def pid(self) -> int:
        assert self.process is not None
        return self.process.pid

    async def kill(self) -> None:
        """SIGKILL the server: no release of leases, no cleanup."""
        if self.process is not None and self.process.returncode is None:
            self.process.send_signal(signal.SIGKILL)
            await self.process.wait()

    async def stop(self) -> None:
        """SIGTERM and wait; SIGKILL if it hangs."""
        if self.process is not None and self.process.returncode is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 20)
            except TimeoutError:
                await self.kill()

    def connect(self, token: str = TOKEN) -> Server:
        return Server.connect(self.address, token=token)

    def cleanup(self) -> None:
        if self._owns_directory:
            shutil.rmtree(self.directory, ignore_errors=True)


@asynccontextmanager
async def serving(
    dsn: str,
    *,
    tenants: dict[str, dict[str, Any]] | None = None,
    worker: dict[str, Any] | None | bool = True,
) -> AsyncIterator[LiveServer]:
    """Run a `LiveServer` for the block; stop it at the end."""
    server = LiveServer(dsn, tenants=tenants, worker=worker)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
        server.cleanup()


async def until(
    check: Callable[[], Awaitable[Any]], *, timeout: float, every: float = 0.5, what: str = "it"
) -> Any:
    """The first truthy answer of `check`, polled until `timeout` seconds."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{what} did not happen within {timeout} s")
        await asyncio.sleep(every)


def final_text(events: list[Envelope]) -> str:
    last = events[-1].event
    assert isinstance(last, ResultEvent), f"the run did not end with a result: {last}"
    return last.final_text or ""


def seqs_increasing(events: list[Envelope]) -> bool:
    """Whether the durable seqs strictly increase: no repeat, no reorder.
    Gaps are by design: each model call's dispatch and receipt records take
    seqs and are never sent to clients (docs/usage/events.md, Sequence numbers)."""
    seqs = [e.seq for e in events]
    return seqs == sorted(set(seqs)) and bool(seqs)


async def once_free(start: Callable[[], Awaitable[Run]]) -> Any:
    """The result of the run `start` continues, retried while a killed
    server's lease on it lives."""
    for _ in range(40):
        run = await start()
        try:
            return await run.result()
        except RunLeased as leased:
            await asyncio.sleep(leased.expires_in or 0.5)
    raise AssertionError("the killed server's lease never ran out")


def ended(jobs: list[Job]) -> list[Job]:
    return [job for job in jobs if job["state"] == "done"]

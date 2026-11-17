"""A recon, per-service checks and a report, directed by a workflow, on a real model.

Skips without `LINCH_LIVE_VLLM_URL` (the server's `/v1` root) and
`LINCH_TEST_PG_DSN`; `LINCH_LIVE_VLLM_MODEL` names the model (the first one the
server lists when absent). One request at a time, small outputs, thinking off.

The "target" is a tiny HTTP server this test starts on 127.0.0.1 and nothing
else is contacted. The workflow's first drive stops after the checks (the
function raises, as a crashed client would); the second drive, on a new
server, must replay the recon and the checks and run only the report. The
assertions are on the journal and the events, never on the model's wording.
"""

from __future__ import annotations

import http.server
import json
import os
import threading
import urllib.request
from collections.abc import Iterator
from typing import Any, cast

import pytest
from test_workflow import kinds, pg_dsn  # noqa: F401

from linch_rust import (
    AgentConfig,
    OutputSchema,
    Server,
    WorkflowContext,
    WorkflowEvent,
    run_workflow,
    workflow_status,
)

URL = os.environ.get("LINCH_LIVE_VLLM_URL")
pytestmark = pytest.mark.skipif(not URL, reason="LINCH_LIVE_VLLM_URL is not set")

SERVICES = {
    "/ssh": "OpenSSH 7.2 banner, password login enabled, root login allowed",
    "/web": "nginx 1.25 with TLS 1.3, HSTS on, no directory listing",
    "/db": "PostgreSQL 16 listening on all interfaces, no password for the postgres user",
}

VERDICT: OutputSchema = {
    "name": "verdict",
    "schema": {
        "type": "object",
        "properties": {
            "risky": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["risky", "reason"],
    },
}


class Target(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/":
            body = json.dumps(sorted(SERVICES)).encode()
        elif self.path in SERVICES:
            body = SERVICES[self.path].encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@pytest.fixture
def target() -> Iterator[str]:
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Target)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def model_name() -> str:
    named = os.environ.get("LINCH_LIVE_VLLM_MODEL")
    if named:
        return named
    with urllib.request.urlopen(f"{URL}/models", timeout=10) as reply:
        return str(json.load(reply)["data"][0]["id"])


def live(dsn: str) -> AgentConfig:
    config: dict[str, Any] = {
        "namespace": "wf-live",
        "spec": {
            "model": model_name(),
            "system": {
                "sections": [
                    {
                        "name": "role",
                        "text": "You are a terse security reviewer. Use only the given facts.",
                        "order": 0,
                    }
                ]
            },
            "max_turns": 3,
            "max_output_tokens": 300,
            "thinking": {"type": "disabled"},
            # A flaky structured answer is asked for again once.
            "structured_output_retries": 1,
        },
        "provider": {"id": "vllm", "base_url": URL},
        "stores": {"type": "postgres", "dsn": dsn, "lease_ttl_ms": 1000},
        "permissions": {"mode": "default", "project_root": "/"},
    }
    return cast(AgentConfig, config)


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as reply:
        return str(reply.read().decode())


class Crashed(Exception):
    pass


async def pentest(wf: WorkflowContext, base: str, crash_after_checks: bool) -> dict[str, Any]:
    await wf.phase("recon")
    # The recon is a step: its result replays, and the checks that follow
    # are built from the journaled value, so a resume takes the same path.
    paths = await wf.step("recon", lambda: json.loads(fetch(base + "/")))
    await wf.phase("checks")

    def check(path: str) -> Any:
        async def run() -> dict[str, Any]:
            banner = await wf.step("banner", lambda: fetch(base + path), key=path)
            answer = await wf.agent_result(
                f"Service {path}: {banner}. Is it risky to expose it?",
                label=path,
                output_schema=VERDICT,
            )
            return {"path": path, **(answer.structured or {})}

        return run

    verdicts = await wf.parallel([check(path) for path in paths])
    if crash_after_checks:
        raise Crashed
    await wf.phase("report")
    report = await wf.agent(
        "Write a two-line report of these findings: " + json.dumps(verdicts),
    )
    return {"paths": paths, "verdicts": verdicts, "report": report}


async def test_a_real_model_directs_recon_checks_and_a_report_across_a_crash(
    pg_dsn: str,  # noqa: F811
    target: str,
) -> None:
    async with Server() as server:
        agent = await server.open_agent(live(pg_dsn))
        with pytest.raises(Crashed):
            await run_workflow(
                agent,
                lambda wf: pentest(wf, target, True),
                run_id="pentest-1",
                max_concurrency=1,
                step_timeout_ms=120_000,
            )
        parked = await workflow_status(agent, "pentest-1")
    # recon + 3 banners + 3 verdicts
    assert (parked.status, parked.records) == ("failed", 7)

    events: list[WorkflowEvent] = []
    async with Server() as server:
        agent = await server.open_agent(live(pg_dsn))
        result = await run_workflow(
            agent,
            lambda wf: pentest(wf, target, False),
            run_id="pentest-1",
            max_concurrency=1,
            step_timeout_ms=120_000,
            on_event=events.append,
        )
        done = await workflow_status(agent, "pentest-1")

    assert result["paths"] == sorted(SERVICES)
    assert [v["path"] for v in result["verdicts"]] == sorted(SERVICES)
    assert all(isinstance(v["risky"], bool) for v in result["verdicts"])
    assert result["report"].strip()
    live_calls = [e for e in events if e.kind == "agent_start"]
    assert [e.title for e in live_calls] == ["agent"], "only the report ran again"
    assert kinds(events).count("agent_replayed") == 3
    assert kinds(events).count("step_replayed") == 4
    assert (done.status, done.records) == ("completed", 8)

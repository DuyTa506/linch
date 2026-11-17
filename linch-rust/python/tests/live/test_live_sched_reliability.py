"""Live: provider retry and model fallback against a real model behind a
tiny local proxy that fails chosen requests (429, 5xx) and forwards the
rest. Skips without the live environment.
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import pytest
from live_sched_support import (
    DEEPSEEK_MODEL,
    VLLM_MODEL,
    VLLM_URL,
    kind,
    live_config,
    model,
)

from linch_rust import (
    ModelFallbackEvent,
    ProviderRetryEvent,
    ResultEvent,
    Server,
    UsageEvent,
)

__all__ = ["kind"]

pytestmark = pytest.mark.live


class Proxy:
    """A proxy in front of a real endpoint. `fail(model, n)` answers the
    n-th chat request (counting every one, from 1) with a status, or None to
    forward it."""

    def __init__(self, upstream: str, fail: Callable[[str, int], int | None]) -> None:
        self.upstream = urlsplit(upstream)
        self.fail = fail
        self.seen: list[tuple[str, int | None]] = []  # (model, status it got; None: forwarded)
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _forward(self, body: bytes | None) -> None:
                up = proxy.upstream
                connection: http.client.HTTPConnection = (
                    http.client.HTTPSConnection(up.netloc, timeout=300)
                    if up.scheme == "https"
                    else http.client.HTTPConnection(up.netloc, timeout=300)
                )
                skip = {"host", "content-length", "connection", "accept-encoding"}
                headers = {k: v for k, v in self.headers.items() if k.lower() not in skip}
                connection.request(self.command, self.path, body, headers)
                response = connection.getresponse()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
                        self.send_header(key, value)
                self.end_headers()
                while chunk := response.read(1024):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                connection.close()

            def do_GET(self) -> None:
                self._forward(None)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                name = str(json.loads(body).get("model", ""))
                number = len(proxy.seen) + 1
                status = proxy.fail(name, number)
                proxy.seen.append((name, status))
                if status is None:
                    self._forward(body)
                    return
                payload = json.dumps({"error": {"message": "injected", "type": "test"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}{self.upstream.path.rstrip('/')}"

    def __enter__(self) -> Proxy:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def upstream_of(kind: str) -> str:
    return VLLM_URL if kind == "vllm" else "https://api.deepseek.com"


def real_model(kind: str) -> str:
    return VLLM_MODEL if kind == "vllm" else DEEPSEEK_MODEL


@pytest.fixture
def proxy_for() -> Iterator[Callable[[str, Callable[[str, int], int | None]], Proxy]]:
    made: list[Proxy] = []

    def make(kind: str, fail: Callable[[str, int], int | None]) -> Proxy:
        proxy = Proxy(upstream_of(kind), fail).__enter__()
        made.append(proxy)
        return proxy

    yield make
    for proxy in made:
        proxy.__exit__()


def through(kind: str, proxy: Proxy, **spec: Any) -> Any:
    name, provider = model(kind)
    config = live_config(kind, "reliability", None, spec={"model": name, **spec})
    config["provider"] = {**provider, "base_url": proxy.url}  # type: ignore[typeddict-item]
    return config


@pytest.mark.parametrize(
    ("status", "usage_complete"),
    [(429, True), (503, True), (500, False)],
    ids=["429", "503", "500-may-have-billed"],
)
async def test_a_retryable_status_is_retried_and_the_run_succeeds(
    kind: str,
    status: int,
    usage_complete: bool,
    proxy_for: Callable[[str, Callable[[str, int], int | None]], Proxy],
) -> None:
    proxy = proxy_for(kind, lambda _model, n: status if n <= 2 else None)
    retry = {"max_attempts": 4, "base_delay_ms": 100, "max_delay_ms": 400, "jitter": 0}
    config = through(kind, proxy, provider_retry=retry)
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("s1", "Reply with the single word PONG.")
        events = [e.event async for e in run]

    retries = [e for e in events if isinstance(e, ProviderRetryEvent)]
    assert [r.retry for r in retries] == [1, 2]
    assert all(r.error["status"] == status for r in retries), retries
    result = events[-1]
    assert isinstance(result, ResultEvent) and result.subtype == "success", result
    assert result.usage_complete is usage_complete
    assert [s for _, s in proxy.seen] == [status, status, None], "two refusals, one real call"
    assert len([e for e in events if isinstance(e, UsageEvent)]) == 1, "only the paid call"


async def test_a_model_that_keeps_failing_falls_back_to_the_next(
    kind: str,
    proxy_for: Callable[[str, Callable[[str, int], int | None]], Proxy],
) -> None:
    good = real_model(kind)
    proxy = proxy_for(kind, lambda name, _n: 503 if name == "primary-down" else None)
    retry = {"max_attempts": 2, "base_delay_ms": 100, "max_delay_ms": 200, "jitter": 0}
    config = through(
        kind, proxy, provider_retry=retry, fallback_models=[good], model="primary-down"
    )
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("s1", "Reply with the single word PONG.")
        events = [e.event async for e in run]
        durable = await agent.events(run.id)

    [fallback] = [e for e in events if isinstance(e, ModelFallbackEvent)]
    assert (fallback.from_model, fallback.to_model) == ("primary-down", good)
    assert fallback.error["status"] == 503
    assert [e.retry for e in events if isinstance(e, ProviderRetryEvent)] == [1]
    [usage] = [e for e in events if isinstance(e, UsageEvent)]
    assert usage.actual_model == good, "the fallback answered"
    result = events[-1]
    assert isinstance(result, ResultEvent) and result.subtype == "success", result
    assert any(isinstance(e.event, ModelFallbackEvent) for e in durable), "the event is durable"
    assert [name for name, _ in proxy.seen] == ["primary-down", "primary-down", good]

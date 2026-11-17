"""Shared pieces of the live core tests: providers, a server, a run driver.

The tests drive a real model through the `linch-server` sidecar. They skip
without `LINCH_LIVE_VLLM_URL` or `LINCH_TEST_VLLM_URL` (vLLM, no key) or
`DEEPSEEK_API_KEY` (DeepSeek).
Assertions are on durable facts (events, tool results, files, rows), never on
the model's wording; a flaky model turn is retried once by `retry_once`.
"""

from __future__ import annotations

import asyncio
import functools
import os
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

import pytest

from linch_rust import (
    Agent,
    Allow,
    AssistantEvent,
    Decision,
    Deny,
    InputRequestEvent,
    PermissionRequestEvent,
    ResultEvent,
    Run,
    Server,
    SuspendedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from linch_rust.agent import Answer
from linch_rust.events import Envelope

VLLM_URL = os.environ.get("LINCH_LIVE_VLLM_URL") or os.environ.get("LINCH_TEST_VLLM_URL")
VLLM_MODEL = (
    os.environ.get("LINCH_LIVE_VLLM_MODEL")
    or os.environ.get("LINCH_TEST_VLLM_MODEL")
    or "qwen3.6-35b-a3b"
)
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = os.environ.get("LINCH_LIVE_DEEPSEEK_MODEL", "deepseek-flash")
PG_DSN = os.environ.get("LINCH_LIVE_PG_DSN") or os.environ.get("LINCH_TEST_PG_DSN")

# A value the server holds in its own environment and no command may see.
CANARY = "LIVECORE-CANARY-" + uuid.uuid4().hex

T = TypeVar("T")


@dataclass(frozen=True)
class Live:
    """One live provider: its `provider` config, model and output limit."""

    name: str
    provider: dict[str, Any]
    model: str
    max_output: int

    def spec(self, **extra: Any) -> dict[str, Any]:
        """A spec with thinking off (fast, cheap); `extra` overrides any field."""
        spec: dict[str, Any] = {
            "model": self.model,
            "max_output_tokens": self.max_output,
            "thinking": {"type": "disabled"},
            "call_timeout_ms": 120_000,
            "system_prompt": "You are a terse test agent. Follow tool instructions exactly.",
        }
        spec.update(extra)
        return spec


VLLM = Live("vllm", {"id": "vllm", "base_url": VLLM_URL or ""}, VLLM_MODEL, 1200)
DEEPSEEK = Live("deepseek", {"id": "deepseek"}, DEEPSEEK_MODEL, 800)

PROVIDERS = [
    pytest.param(
        VLLM,
        id="vllm",
        marks=pytest.mark.skipif(not VLLM_URL, reason="LINCH_LIVE_VLLM_URL is not set"),
    ),
    pytest.param(
        DEEPSEEK,
        id="deepseek",
        marks=pytest.mark.skipif(not DEEPSEEK_KEY, reason="DEEPSEEK_API_KEY is not set"),
    ),
]
ONLY_VLLM = [PROVIDERS[0]]
ONLY_DEEPSEEK = [PROVIDERS[1]]

needs_live = pytest.mark.skipif(
    not (VLLM_URL or DEEPSEEK_KEY),
    reason="no live model: set LINCH_LIVE_VLLM_URL or DEEPSEEK_API_KEY",
)
needs_pg = pytest.mark.skipif(not PG_DSN, reason="LINCH_TEST_PG_DSN is not set")


def server_env(**extra: str) -> dict[str, str]:
    """The sidecar's whole environment: PATH, HOME, the key, and the canary."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LIVECORE_CANARY": CANARY,
    }
    if DEEPSEEK_KEY:
        env["DEEPSEEK_API_KEY"] = DEEPSEEK_KEY
    env.update(extra)
    return env


@asynccontextmanager
async def live_server(**extra_env: str) -> AsyncIterator[Server]:
    async with Server(env=server_env(**extra_env)) as server:
        yield server


def namespace(prefix: str = "livecore") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def make_config(live: Live, spec: dict[str, Any] | None = None, **top: Any) -> Any:
    """An agent config for `live` with a fresh namespace; `top` adds top-level fields."""
    config: dict[str, Any] = {
        "namespace": namespace(),
        "spec": spec if spec is not None else live.spec(),
        "provider": live.provider,
    }
    config.update(top)
    return config


def pg_stores() -> dict[str, Any]:
    assert PG_DSN
    return {"type": "postgres", "dsn": PG_DSN}


def retry_once(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Run a scenario again once when an assertion fails.

    A live model sometimes answers a deterministic prompt with a different
    tool order or none. The retry is announced on stderr so a flaky
    scenario is visible; two failures in a row fail the test.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return await fn(*args, **kwargs)
        except (AssertionError, ValueError, IndexError, KeyError) as error:
            print(
                f"[live retry] {fn.__name__}: {type(error).__name__} {str(error)[:300]}",
                file=sys.stderr,
            )
            return await fn(*args, **kwargs)

    return wrapper


@dataclass
class Trace:
    """Every envelope of a run across its suspends and resumes."""

    envelopes: list[Envelope] = field(default_factory=list[Envelope])

    @property
    def events(self) -> list[Any]:
        return [e.event for e in self.envelopes]

    def of(self, kind: type[T]) -> list[T]:
        return [e for e in self.events if isinstance(e, kind)]

    @property
    def result(self) -> ResultEvent:
        results = self.of(ResultEvent)
        assert results, f"run ended without a result: {[e.type for e in self.events][-8:]}"
        return results[-1]

    def starts(self, name: str | None = None) -> list[ToolCallStartEvent]:
        return [s for s in self.of(ToolCallStartEvent) if name in (None, s.tool_name)]

    def ends(self, name: str | None = None) -> list[ToolCallEndEvent]:
        return [e for e in self.of(ToolCallEndEvent) if name in (None, e.tool_name)]

    def assistants(self) -> list[AssistantEvent]:
        return self.of(AssistantEvent)


def first(items: list[T], what: str) -> T:
    """The first of `items`; a model that skipped the step fails an assertion (retried)."""
    assert items, f"the model never produced {what}"
    return items[0]


def text_of(end: ToolCallEndEvent) -> str:
    """The text of a tool result block."""
    return "".join(part.get("text", "") for part in end.result.get("content", []))


def message_text(message: dict[str, Any]) -> str:
    return "".join(b.get("text", "") for b in message.get("content", []) if b.get("type") == "text")


def allow_all(_request: dict[str, Any]) -> Decision:
    return Allow()


async def drive(
    agent: Agent,
    run: Run,
    *,
    decide: Callable[[dict[str, Any]], Decision] = allow_all,
    answers: Callable[[InputRequestEvent], Answer] | None = None,
    timeout: float = 300,
    max_rounds: int = 8,
) -> Trace:
    """Iterate a run to its result, answering approvals and questions on the way."""
    trace = Trace()
    async with asyncio.timeout(timeout):
        for _ in range(max_rounds):
            requests: list[dict[str, Any]] = []
            inputs: list[InputRequestEvent] = []
            async for envelope in run:
                trace.envelopes.append(envelope)
                event = envelope.event
                if isinstance(event, PermissionRequestEvent):
                    requests.extend(event.requests)
                elif isinstance(event, InputRequestEvent):
                    inputs.append(event)
            last = trace.events[-1]
            if not isinstance(last, SuspendedEvent):
                return trace
            if inputs:
                assert answers is not None, "the run asked a question and no answer was given"
                run = await agent.answer(run.id, {i.request_id: answers(i) for i in inputs})
            else:
                decisions = {r["tool_use_id"]: decide(r) for r in requests}
                run = await agent.approve(run.id, decisions)
    raise AssertionError("run did not finish within max_rounds suspends")


async def ask(
    live: Live,
    prompt: str,
    *,
    config: Any = None,
    tools: tuple[Any, ...] = (),
    callbacks: tuple[Any, ...] = (),
    session: str = "s1",
    decide: Callable[[dict[str, Any]], Decision] = allow_all,
    answers: Callable[[InputRequestEvent], Answer] | None = None,
    server_env_extra: dict[str, str] | None = None,
    **run_kwargs: Any,
) -> Trace:
    """Open one agent on a fresh server, run `prompt` to its result, close."""
    async with live_server(**(server_env_extra or {})) as server:
        agent = await server.open_agent(
            config if config is not None else make_config(live),
            tools=list(tools),
            callbacks=list(callbacks),
        )
        run = await agent.run(session, prompt, **run_kwargs)
        return await drive(agent, run, decide=decide, answers=answers)


__all__ = [
    "CANARY",
    "DEEPSEEK",
    "ONLY_DEEPSEEK",
    "ONLY_VLLM",
    "PROVIDERS",
    "VLLM",
    "Deny",
    "Live",
    "Trace",
    "ask",
    "drive",
    "first",
    "live_server",
    "make_config",
    "message_text",
    "needs_live",
    "needs_pg",
    "pg_stores",
    "retry_once",
    "server_env",
    "text_of",
]

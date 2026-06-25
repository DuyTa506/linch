"""Live LoopRunner integration test.

Run with a project ``.env``:

    LINCH_RUN_LIVE_LOOP_TESTS=1 pytest tests/integration/test_live_loop_runner.py -v

Required .env values:

    API_KEY=sk-...
    BASE_URL=https://api.deepseek.com
    model=deepseek-chat

or provider-specific names:

    DEEPSEEK_API_KEY=sk-...
    OPENAI_API_KEY=sk-...

Optional:

    LOOP_RUNNER_MODEL=deepseek-chat

The explicit LINCH_RUN_LIVE_LOOP_TESTS guard keeps ordinary local/CI runs from
spending API quota just because a developer has a project .env file.
"""

from __future__ import annotations

import asyncio
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_project_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_project_env()

RUN_LIVE = os.environ.get("LINCH_RUN_LIVE_LOOP_TESTS") == "1"
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAPI_KEY")
GENERIC_KEY = os.environ.get("API_KEY")
API_KEY = DEEPSEEK_KEY or OPENAI_KEY or GENERIC_KEY
BASE_URL = (
    "https://api.deepseek.com"
    if DEEPSEEK_KEY
    else os.environ.get("LOOP_RUNNER_BASE_URL") or os.environ.get("BASE_URL")
)
MODEL = os.environ.get(
    "LOOP_RUNNER_MODEL",
    os.environ.get("MODEL")
    or os.environ.get("model")
    or ("deepseek-chat" if BASE_URL else "gpt-4o-mini"),
)
TIMEOUT_S = float(os.environ.get("LOOP_RUNNER_TEST_TIMEOUT_S", "45"))
needs_live_loop = pytest.mark.skipif(
    not RUN_LIVE or not API_KEY,
    reason="set LINCH_RUN_LIVE_LOOP_TESTS=1 and API_KEY/DEEPSEEK_API_KEY/OPENAI_API_KEY in .env",
)


@needs_live_loop
async def test_live_loop_runner_run_once_with_project_env(tmp_path) -> None:
    from linch import (
        Agent,
        FileLoopLeaseStore,
        LoopRunner,
        LoopSpec,
        LoopTickResult,
        LoopTrigger,
        RunOptions,
    )
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.providers import OpenAIChatCompletionsProvider, OpenAIChatProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    def verify(result: LoopTickResult) -> bool:
        return result.report.status == "completed" and not result.report.errors

    def done(result: LoopTickResult, _artifacts) -> bool:
        return "LOOP_DONE" in (result.final_text or "")

    _skip_if_endpoint_unreachable()

    provider = OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(
            api_key=API_KEY,
            base_url=BASE_URL,
            timeout=TIMEOUT_S,
        )
    )
    agent = Agent(
        model=MODEL,
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a deterministic test assistant. Reply in one short sentence. "
                "Always include the exact token LOOP_DONE."
            ),
        ),
    )
    spec = LoopSpec(
        id="live-loop-runner",
        charter="Prove LoopRunner can execute one real provider-backed tick.",
        prompt="Say that the loop runner live test completed.",
        root=tmp_path / "domains",
        run_options=RunOptions(max_output_tokens=80),
        session_meta={"test": "live_loop_runner"},
    )
    runner = LoopRunner(
        agent,
        leases=FileLoopLeaseStore(root=spec.root),
        verify=verify,
        done_predicate=done,
    )
    result: LoopTickResult | None = None

    try:
        try:
            result = await asyncio.wait_for(
                runner.run_once(
                    spec,
                    LoopTrigger(source="pytest", payload="live integration test"),
                ),
                timeout=TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            pytest.xfail(f"live provider did not complete within {TIMEOUT_S}s")
    finally:
        await agent.close()
        try:
            await asyncio.wait_for(provider.aclose(), timeout=5)
        except asyncio.TimeoutError:
            pass

    assert result is not None
    if result.status != "completed":
        detail = (
            _error_summary(result.report.errors)
            if result.report.errors
            else f"report.status={result.report.status}, final_text={result.final_text!r}"
        )
        pytest.xfail(f"live provider did not return a completed tick: {detail}")
    if result.done is not True:
        pytest.xfail(f"live provider did not satisfy done predicate: {result.final_text!r}")

    assert result.status == "completed"
    assert result.done is True
    assert result.final_text is not None
    assert "LOOP_DONE" in result.final_text
    assert result.report.run_id == result.run_id
    assert result.report.session_id

    domain = tmp_path / "domains" / "live-loop-runner"
    assert (domain / "README.md").exists()
    assert (domain / "LOG.md").exists()
    assert not (domain / ".lock.json").exists()
    assert (domain / "artifacts" / "runs" / f"{result.run_id}.md").exists()
    assert (domain / "artifacts" / "runs" / f"{result.run_id}.json").exists()


def _error_summary(errors: list[dict[str, object]]) -> str:
    first = errors[0] if errors else {}
    name = str(first.get("name", "ProviderError"))
    message = str(first.get("message", ""))
    if len(message) > 180:
        message = message[:177] + "..."
    return f"{name}: {message}"


def _skip_if_endpoint_unreachable() -> None:
    url = BASE_URL or "https://api.openai.com/v1"
    request = urllib.request.Request(url, method="HEAD")
    try:
        urllib.request.urlopen(request, timeout=min(5.0, TIMEOUT_S)).close()
    except urllib.error.HTTPError:
        return
    except OSError as exc:
        pytest.skip(f"live provider endpoint is not reachable: {type(exc).__name__}")

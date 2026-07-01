from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from linch import Agent, ContextBuildResult  # noqa: E402
from linch.compaction import CompactionLadder  # noqa: E402
from linch.config import FeatureFlags, SystemPromptConfig, SystemPromptSection  # noqa: E402
from linch.hooks import ContextInjectionHook, HookResult  # noqa: E402
from linch.providers import (  # noqa: E402
    AnthropicProvider,
    AnthropicProviderOptions,
    OpenAIChatCompletionsProvider,
    OpenAIChatProviderOptions,
)
from linch.sessions import InMemorySessionStore  # noqa: E402
from linch.tools import ToolContext, ToolRegistry, ToolResult  # noqa: E402
from linch.types import (  # noqa: E402
    Message,
    ProviderRequest,
    SystemBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

ProviderChoice = Literal["openai-chat", "anthropic", "both"]
OutputFormat = Literal["markdown", "json"]
BenchmarkMode = Literal["direct", "tool-loop"]
BenchmarkScenario = Literal[
    "baseline",
    "stable-context",
    "volatile-context",
    "rotating-tools",
    "compaction",
    "model-steering",
]

SCENARIOS: tuple[BenchmarkScenario, ...] = (
    "baseline",
    "stable-context",
    "volatile-context",
    "rotating-tools",
    "compaction",
    "model-steering",
)


@dataclass(slots=True)
class LiveCacheCall:
    call: int
    elapsed_ms: float
    stop_reason: str | None = None
    text: str = ""
    thinking_chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    error_type: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "call": self.call,
            "elapsed_ms": self.elapsed_ms,
            "stop_reason": self.stop_reason,
            "text": self.text,
            "thinking_chars": self.thinking_chars,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_creation_tokens": self.cache_creation_tokens,
            },
        }
        if self.error_type is not None:
            data["error_type"] = self.error_type
            data["error"] = self.error
        return data


@dataclass(slots=True)
class LiveCacheResult:
    provider: str
    model: str
    mode: str = "direct"
    scenario: str = "baseline"
    tool_calls: int = 0
    compactions: int = 0
    elapsed_ms: float = 0.0
    calls: list[LiveCacheCall] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        return {
            "input_tokens": sum(call.input_tokens for call in self.calls),
            "output_tokens": sum(call.output_tokens for call in self.calls),
            "cache_read_tokens": sum(call.cache_read_tokens for call in self.calls),
            "cache_creation_tokens": sum(call.cache_creation_tokens for call in self.calls),
        }

    @property
    def estimated_prompt_tokens(self) -> int:
        return sum(_estimated_prompt_tokens(self.provider, call) for call in self.calls)

    @property
    def cache_read_ratio(self) -> float:
        prompt_total = self.estimated_prompt_tokens
        cache_read = self.totals["cache_read_tokens"]
        return round(cache_read / prompt_total, 4) if prompt_total else 0.0

    @property
    def warm_cache_read_ratio(self) -> float:
        warm_calls = [call for call in self.calls[1:] if call.error_type is None]
        prompt_total = sum(_estimated_prompt_tokens(self.provider, call) for call in warm_calls)
        cache_read = sum(call.cache_read_tokens for call in warm_calls)
        return round(cache_read / prompt_total, 4) if prompt_total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "mode": self.mode,
            "scenario": self.scenario,
            "tool_calls": self.tool_calls,
            "compactions": self.compactions,
            "elapsed_ms": self.elapsed_ms,
            "totals": self.totals,
            "estimated_prompt_tokens": self.estimated_prompt_tokens,
            "cache_read_ratio": self.cache_read_ratio,
            "warm_cache_read_ratio": self.warm_cache_read_ratio,
            "calls": [call.to_dict() for call in self.calls],
        }


class CacheProbeTool:
    name = "CacheProbe"
    description = (
        "Benchmark-only read tool. Call once per requested benchmark step, in order, "
        "then answer exactly OK after the final step."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "step": {
                "type": "integer",
                "description": "The benchmark step number, starting at 1.",
            }
        },
        "required": ["step"],
    }
    scope = "read"
    parallel = False

    def __init__(self, target_steps: int, *, payload_chars: int = 0) -> None:
        self.target_steps = target_steps
        self.payload_chars = max(0, payload_chars)
        self.calls: list[int] = []

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        step = raw.get("step", len(self.calls) + 1)
        try:
            parsed = int(step)
        except (TypeError, ValueError):
            parsed = len(self.calls) + 1
        return {"step": parsed}

    def summarize(self, input: dict[str, Any]) -> str:
        return f"cache probe step {input.get('step')}"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        step = int(input.get("step", len(self.calls) + 1))
        self.calls.append(step)
        if len(self.calls) >= self.target_steps:
            instruction = "All benchmark tool steps are complete. Answer exactly OK now."
        else:
            next_step = len(self.calls) + 1
            instruction = f"Call CacheProbe again with step={next_step}."
        payload = ""
        if self.payload_chars:
            payload = "\nPayload:\n" + ("x" * self.payload_chars)
        content = (
            f"CacheProbe observed step={step}. Completed {len(self.calls)}/"
            f"{self.target_steps}. {instruction}{payload}"
        )
        return ToolResult(content=content, summary=f"step {step}")


class ExtraProbeTool:
    name = "ExtraProbe"
    description = "Benchmark-only extra read tool used to change the available tool schema set."
    input_schema = {"type": "object", "properties": {}}
    scope = "read"
    parallel = False

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {}

    def summarize(self, input: dict[str, Any]) -> str:
        return "extra probe"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(content="ExtraProbe should not be needed.", summary="extra")


class StableContextBuilder:
    async def build(self, turn: Any) -> ContextBuildResult:
        return ContextBuildResult(
            messages=[
                Message(
                    role="user",
                    content=[TextBlock(text="<benchmark-context>stable</benchmark-context>")],
                )
            ],
            metadata={"benchmark_context": "stable"},
        )


class VolatileContextBuilder:
    async def build(self, turn: Any) -> ContextBuildResult:
        return ContextBuildResult(
            messages=[
                Message(
                    role="user",
                    content=[
                        TextBlock(
                            text=(
                                "<benchmark-context>"
                                f"volatile-turn-{turn.turn_index}"
                                "</benchmark-context>"
                            )
                        )
                    ],
                )
            ],
            metadata={"benchmark_context": "volatile"},
        )


class RotatingToolSelectionBuilder:
    async def build(self, turn: Any) -> ContextBuildResult:
        names = ["CacheProbe", "ExtraProbe"] if turn.turn_index % 2 == 0 else ["CacheProbe"]
        return ContextBuildResult(
            selected_tools=names,
            metadata={"benchmark_selected_tools": names},
        )


class ModelSteeringHook:
    name = "benchmark_model_steering"

    def __init__(self, base_model: str, steering_model: str) -> None:
        self.base_model = base_model
        self.steering_model = steering_model

    def on_before_provider_call(self, ctx: Any) -> HookResult | None:
        req = getattr(ctx, "request", None)
        if req is None:
            return None
        turn_index = int(getattr(ctx, "turn_index", 0) or 0)
        req.model = self.steering_model if turn_index % 2 else self.base_model
        return HookResult.mutate(request=req)


class ContextWindowOverrideProvider:
    def __init__(self, provider: Any, context_window: int) -> None:
        self.provider = provider
        self.context_window_override = context_window
        self.id = getattr(provider, "id", "wrapped")

    def context_window(self, model: str) -> int:
        return self.context_window_override

    def capabilities(self, model: str) -> Any:
        capabilities = getattr(self.provider, "capabilities", None)
        if not callable(capabilities):
            return None
        caps = capabilities(model)
        try:
            return replace(caps, context_window=self.context_window_override)
        except TypeError:
            return caps

    async def stream(self, req: ProviderRequest) -> Any:
        async for event in self.provider.stream(req):
            yield event


class BenchmarkCompaction:
    id = "benchmark-keep-recent-1"

    async def compact(self, ctx: Any, provider: Any) -> list[Message]:
        recent = _last_n_assistant_turns(ctx.messages, 1)
        return [
            Message(
                role="user",
                content=[
                    TextBlock(
                        text=(
                            "<benchmark compaction summary>\n"
                            "Earlier benchmark tool-loop messages were compacted."
                        )
                    )
                ],
            ),
            *recent,
        ]


def _last_n_assistant_turns(messages: list[Message], n: int) -> list[Message]:
    assistants_found = 0
    boundary = 0
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "assistant":
            assistants_found += 1
            if assistants_found == n:
                boundary = index
                break
    if assistants_found < n:
        return list(messages)
    return list(messages[boundary:])


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    env_path = Path(path)
    values: dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return {**values, **os.environ}


def build_static_prefix(lines: int, *, salt: str = "") -> str:
    salt_line = f"Benchmark prefix salt: {salt}\n" if salt else ""
    return (
        "Stable cache benchmark policy. Keep this prefix byte-identical across calls.\n"
        + salt_line
        + "\n".join(
            f"cache-line-{index:04d}: deterministic benchmark content for prefix caching."
            for index in range(lines)
        )
    )


def benchmark_token_estimator(messages: list[Message], model: str) -> int:
    chars = 0
    for message in messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                chars += len(block.text)
            elif isinstance(block, ToolResultBlock):
                content = block.content
                if isinstance(content, str):
                    chars += len(content)
                else:
                    chars += sum(len(part.text) for part in content if isinstance(part, TextBlock))
            elif isinstance(block, ToolUseBlock):
                chars += len(block.name) + len(json.dumps(block.input, sort_keys=True))
    return max(0, chars // 4)


def _estimated_prompt_tokens(provider: str, call: LiveCacheCall) -> int:
    """Normalize provider usage accounting for cache ratios.

    OpenAI-compatible APIs report total prompt tokens in ``input_tokens`` and cached
    tokens as a detail, so adding them double-counts. Anthropic-compatible usage
    reports cached input separately, so the prompt denominator is input + cache.
    """

    if call.error_type is not None:
        return 0
    if provider in {"openai-chat", "vllm", "sglang", "llamacpp"}:
        return max(0, call.input_tokens)
    return max(0, call.input_tokens + call.cache_read_tokens + call.cache_creation_tokens)


async def _stream_call(
    provider: Any,
    *,
    model: str,
    static_prefix: str,
    call_index: int,
    max_output_tokens: int,
    timeout_s: float,
) -> LiveCacheCall:
    started = time.perf_counter()
    req = ProviderRequest(
        model=model,
        system=[SystemBlock(text=static_prefix, cacheable=True)],
        tools=[],
        messages=[
            Message(
                role="user",
                content=[TextBlock(text=f"Reply with exactly OK. Run {call_index}.")],
            )
        ],
        max_output_tokens=max_output_tokens,
        cache_prompt=True,
        cache_ttl="1h",
    )
    text: list[str] = []
    thinking_chars = 0
    usage = None
    stop_reason = None

    async def collect() -> None:
        nonlocal thinking_chars, stop_reason, usage
        async for event in provider.stream(req):
            typ = event.get("type")
            if typ == "text_delta":
                text.append(str(event.get("text", "")))
            elif typ == "thinking_delta":
                thinking_chars += len(str(event.get("text", "")))
            elif typ == "message_end":
                usage = event.get("usage")
                stop_reason = str(event.get("stop_reason") or "")

    try:
        await asyncio.wait_for(collect(), timeout=timeout_s)
    except Exception as exc:
        return LiveCacheCall(
            call=call_index,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )

    return LiveCacheCall(
        call=call_index,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        stop_reason=stop_reason or None,
        text="".join(text).strip()[:120],
        thinking_chars=thinking_chars,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
        cache_creation_tokens=int(getattr(usage, "cache_creation_tokens", 0) or 0),
    )


async def run_provider_benchmark(
    provider_name: str,
    provider: Any,
    *,
    model: str,
    static_prefix: str,
    calls: int,
    max_output_tokens: int,
    timeout_s: float,
    sleep_s: float,
) -> LiveCacheResult:
    result = LiveCacheResult(provider=provider_name, model=model, mode="direct")
    for call_index in range(1, calls + 1):
        row = await _stream_call(
            provider,
            model=model,
            static_prefix=static_prefix,
            call_index=call_index,
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
        )
        result.calls.append(row)
        if row.error_type is not None:
            break
        if sleep_s > 0 and call_index < calls:
            await asyncio.sleep(sleep_s)
    return result


async def run_tool_loop_benchmark(
    provider_name: str,
    provider: Any,
    *,
    model: str,
    static_prefix: str,
    tool_turns: int,
    max_output_tokens: int,
    timeout_s: float,
    scenario: BenchmarkScenario,
    steering_model: str | None,
) -> LiveCacheResult:
    payload_chars = 24000 if scenario == "compaction" else 0
    tool = CacheProbeTool(tool_turns, payload_chars=payload_chars)
    registry = ToolRegistry()
    registry.register(tool)
    hooks: list[Any] = []
    compaction_ladder = None
    provider_for_agent = provider
    if scenario == "stable-context":
        hooks.append(ContextInjectionHook(StableContextBuilder()))
    elif scenario == "volatile-context":
        hooks.append(ContextInjectionHook(VolatileContextBuilder()))
    elif scenario == "rotating-tools":
        registry.register(ExtraProbeTool())
        hooks.append(ContextInjectionHook(RotatingToolSelectionBuilder()))
    elif scenario == "compaction":
        provider_for_agent = ContextWindowOverrideProvider(provider, context_window=8_000)
        compaction_ladder = CompactionLadder(keep_recent_turns=1)
    elif scenario == "model-steering":
        if steering_model and steering_model != model:
            hooks.append(ModelSteeringHook(model, steering_model))

    agent = Agent(
        model=model,
        provider=provider_for_agent,
        tools=registry,
        hooks=hooks,
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False, filesystem=False),
        system_prompt_config=SystemPromptConfig(
            sections=[
                SystemPromptSection(
                    name="cache-benchmark-prefix",
                    text=static_prefix,
                    cacheable=True,
                )
            ]
        ),
        max_output_tokens=max_output_tokens,
        max_turns=tool_turns + 3,
        compaction=BenchmarkCompaction() if scenario == "compaction" else None,
        compaction_ladder=compaction_ladder,
        token_estimator=benchmark_token_estimator if scenario == "compaction" else None,
        result_offload=None,
    )
    session = await agent.session()
    result = LiveCacheResult(
        provider=provider_name,
        model=model,
        mode="tool-loop",
        scenario=scenario,
    )
    started = time.perf_counter()
    prompt = (
        f"Run a prompt-cache benchmark. Call CacheProbe exactly {tool_turns} times, "
        "one call per assistant turn, with step numbers 1 through "
        f"{tool_turns}. After the final tool result, answer exactly OK."
    )

    async def collect() -> None:
        async for event in session.run(prompt):
            event_type = getattr(event, "type", "")
            if event_type == "usage":
                usage = event.usage
                result.calls.append(
                    LiveCacheCall(
                        call=len(result.calls) + 1,
                        elapsed_ms=0.0,
                        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                        cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
                        cache_creation_tokens=int(getattr(usage, "cache_creation_tokens", 0) or 0),
                    )
                )
            elif event_type == "tool_call_end":
                result.tool_calls += 1
            elif event_type == "compaction":
                result.compactions += 1

    try:
        await asyncio.wait_for(collect(), timeout=timeout_s)
    except Exception as exc:
        result.calls.append(
            LiveCacheCall(
                call=len(result.calls) + 1,
                elapsed_ms=0.0,
                error_type=type(exc).__name__,
                error=str(exc)[:300],
            )
        )
    result.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return result


async def run_live_cache_benchmark(
    env: Mapping[str, str],
    *,
    provider_choice: ProviderChoice = "both",
    mode: BenchmarkMode = "direct",
    scenario: BenchmarkScenario = "baseline",
    calls: int = 4,
    tool_turns: int = 3,
    prefix_lines: int = 900,
    prefix_salt: str = "",
    shared_prefix: bool = False,
    max_output_tokens: int = 128,
    timeout_s: float = 35.0,
    sleep_s: float = 1.0,
    steering_model: str | None = None,
) -> list[LiveCacheResult]:
    api_key = env.get("API_KEY") or env.get("DEEPSEEK_API_KEY") or env.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Set API_KEY, DEEPSEEK_API_KEY, or OPENAI_API_KEY in .env.")
    model = env.get("model") or env.get("MODEL") or "deepseek-chat"

    def static_prefix_for(provider_name: str) -> str:
        salt_parts = [
            part for part in (prefix_salt, f"mode={mode}", f"scenario={scenario}") if part
        ]
        if shared_prefix:
            return build_static_prefix(prefix_lines, salt="|".join(salt_parts))
        salt_parts.append(f"provider={provider_name}")
        return build_static_prefix(prefix_lines, salt="|".join(salt_parts))

    results: list[LiveCacheResult] = []
    if provider_choice in {"openai-chat", "both"}:
        base_url = env.get("BASE_URL")
        if base_url:
            results.append(
                await _run_selected_mode(
                    provider_name="openai-chat",
                    provider=OpenAIChatCompletionsProvider(
                        OpenAIChatProviderOptions(
                            api_key=api_key,
                            base_url=base_url,
                            timeout=timeout_s,
                        )
                    ),
                    mode=mode,
                    scenario=scenario,
                    model=model,
                    static_prefix=static_prefix_for("openai-chat"),
                    calls=calls,
                    tool_turns=tool_turns,
                    max_output_tokens=max_output_tokens,
                    timeout_s=timeout_s,
                    sleep_s=sleep_s,
                    steering_model=steering_model,
                )
            )
    if provider_choice in {"anthropic", "both"}:
        base_url = env.get("ANTHROPIC_BASE_URL")
        if base_url:
            results.append(
                await _run_selected_mode(
                    provider_name="anthropic",
                    provider=AnthropicProvider(
                        AnthropicProviderOptions(api_key=api_key, base_url=base_url)
                    ),
                    mode=mode,
                    scenario=scenario,
                    model=model,
                    static_prefix=static_prefix_for("anthropic"),
                    calls=calls,
                    tool_turns=tool_turns,
                    max_output_tokens=max_output_tokens,
                    timeout_s=timeout_s,
                    sleep_s=sleep_s,
                    steering_model=steering_model,
                )
            )
    return results


async def _run_selected_mode(
    *,
    provider_name: str,
    provider: Any,
    mode: BenchmarkMode,
    scenario: BenchmarkScenario,
    model: str,
    static_prefix: str,
    calls: int,
    tool_turns: int,
    max_output_tokens: int,
    timeout_s: float,
    sleep_s: float,
    steering_model: str | None,
) -> LiveCacheResult:
    if mode == "tool-loop":
        return await run_tool_loop_benchmark(
            provider_name,
            provider,
            model=model,
            static_prefix=static_prefix,
            tool_turns=tool_turns,
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
            scenario=scenario,
            steering_model=steering_model,
        )
    return await run_provider_benchmark(
        provider_name,
        provider,
        model=model,
        static_prefix=static_prefix,
        calls=calls,
        max_output_tokens=max_output_tokens,
        timeout_s=timeout_s,
        sleep_s=sleep_s,
    )


def render_json(
    results: Sequence[LiveCacheResult],
    *,
    prefix_lines: int,
    prefix_salt: str,
    shared_prefix: bool,
    mode: BenchmarkMode,
    scenario: BenchmarkScenario,
) -> str:
    return json.dumps(
        {
            "kind": "linch_live_prompt_cache_benchmark",
            "prefix_lines": prefix_lines,
            "prefix_salt_set": bool(prefix_salt),
            "shared_prefix": shared_prefix,
            "mode": mode,
            "scenario": scenario,
            "results": [result.to_dict() for result in results],
        },
        indent=2,
        sort_keys=True,
    )


def render_markdown(
    results: Sequence[LiveCacheResult],
    *,
    prefix_lines: int,
    prefix_salt: str,
    shared_prefix: bool,
    mode: BenchmarkMode,
    scenario: BenchmarkScenario,
) -> str:
    lines = [
        "# Linch Live Prompt Cache Benchmark",
        "",
        "Measures real provider `Usage.cache_read_tokens` and ",
        "`Usage.cache_creation_tokens` across repeated calls with a stable prefix.",
        "",
        f"- prefix_lines: {prefix_lines}",
        f"- prefix_salt_set: {bool(prefix_salt)}",
        f"- shared_prefix: {shared_prefix}",
        f"- mode: {mode}",
        f"- scenario: {scenario}",
        "",
        "| Provider | Scenario | Mode | Provider Calls | Tool Calls | Compactions | "
        "Prompt Est | Input | Cache Read | Cache Write | Read Ratio | Warm Read Ratio |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        totals = result.totals
        lines.append(
            f"| {result.provider} | {result.scenario} | {result.mode} | "
            f"{len(result.calls)} | {result.tool_calls} | {result.compactions} | "
            f"{result.estimated_prompt_tokens} | {totals['input_tokens']} | "
            f"{totals['cache_read_tokens']} | {totals['cache_creation_tokens']} | "
            f"{result.cache_read_ratio:.2%} | {result.warm_cache_read_ratio:.2%} |"
        )
    lines.extend(["", "## Per-call"])
    for result in results:
        lines.append(f"### {result.provider} / {result.scenario}")
        lines.append("")
        lines.append("| Call | ms | input | cache_read | cache_write | text |")
        lines.append("|---:|---:|---:|---:|---:|---|")
        for call in result.calls:
            if call.error_type:
                error_text = _table_cell(f"{call.error_type}: {call.error or ''}")
                lines.append(f"| {call.call} | {call.elapsed_ms:.1f} | 0 | 0 | 0 | {error_text} |")
                continue
            lines.append(
                f"| {call.call} | {call.elapsed_ms:.1f} | {call.input_tokens} | "
                f"{call.cache_read_tokens} | {call.cache_creation_tokens} | "
                f"{_table_cell(call.text)} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def _table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark live provider prompt-cache usage.")
    parser.add_argument("--env-file", default=".env", help="Env file with API_KEY/base URLs.")
    parser.add_argument(
        "--provider",
        choices=["openai-chat", "anthropic", "both"],
        default="both",
        help="Provider endpoint to benchmark.",
    )
    parser.add_argument("--calls", type=int, default=4, help="Repeated calls per provider.")
    parser.add_argument(
        "--mode",
        choices=["direct", "tool-loop"],
        default="direct",
        help="direct repeats provider calls; tool-loop runs a real Agent tool loop.",
    )
    parser.add_argument(
        "--scenario",
        choices=[
            "all",
            "baseline",
            "stable-context",
            "volatile-context",
            "rotating-tools",
            "compaction",
            "model-steering",
        ],
        default="baseline",
        help="Tool-loop scenario to run.",
    )
    parser.add_argument(
        "--steering-model",
        default=None,
        help="Alternate model for --scenario model-steering.",
    )
    parser.add_argument(
        "--tool-turns",
        type=int,
        default=3,
        help="Target number of tool calls when --mode tool-loop is used.",
    )
    parser.add_argument(
        "--prefix-lines",
        type=int,
        default=900,
        help="Stable prefix size. Larger values make cache hits easier to observe.",
    )
    parser.add_argument(
        "--prefix-salt",
        default="",
        help="Optional salt appended to the stable prefix. Use a new value for a cold-ish run.",
    )
    parser.add_argument(
        "--shared-prefix",
        action="store_true",
        help=(
            "Use exactly the same prefix across provider endpoints. By default, "
            "provider name is included in the prefix so provider comparisons are isolated."
        ),
    )
    parser.add_argument("--max-output-tokens", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=35.0)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    return parser


async def _main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.calls < 1:
        raise SystemExit("--calls must be at least 1")
    if args.tool_turns < 1:
        raise SystemExit("--tool-turns must be at least 1")
    if args.prefix_lines < 1:
        raise SystemExit("--prefix-lines must be at least 1")
    env = load_dotenv(args.env_file)
    steering_model = (
        args.steering_model
        or env.get("STEERING_MODEL")
        or env.get("ALT_MODEL")
        or env.get("MODEL_STEERING_MODEL")
    )
    results: list[LiveCacheResult] = []
    scenarios: Sequence[BenchmarkScenario]
    if args.scenario == "all":
        scenarios = SCENARIOS if args.mode == "tool-loop" else ("baseline",)
    else:
        scenarios = (args.scenario,)
    for scenario in scenarios:
        results.extend(
            await run_live_cache_benchmark(
                env,
                provider_choice=args.provider,
                mode=args.mode,
                scenario=scenario,
                calls=args.calls,
                tool_turns=args.tool_turns,
                prefix_lines=args.prefix_lines,
                prefix_salt=args.prefix_salt,
                shared_prefix=args.shared_prefix,
                max_output_tokens=args.max_output_tokens,
                timeout_s=args.timeout,
                sleep_s=args.sleep,
                steering_model=steering_model,
            )
        )
    scenario_label = args.scenario
    if args.format == "json":
        print(
            render_json(
                results,
                prefix_lines=args.prefix_lines,
                prefix_salt=args.prefix_salt,
                shared_prefix=args.shared_prefix,
                mode=args.mode,
                scenario=scenario_label,
            )
        )
    else:
        print(
            render_markdown(
                results,
                prefix_lines=args.prefix_lines,
                prefix_salt=args.prefix_salt,
                shared_prefix=args.shared_prefix,
                mode=args.mode,
                scenario=scenario_label,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

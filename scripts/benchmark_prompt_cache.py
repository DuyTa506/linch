from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from linch import Agent, ContextBuildResult  # noqa: E402
from linch.config import FeatureFlags  # noqa: E402
from linch.hooks import ContextInjectionHook  # noqa: E402
from linch.providers.base import BaseProvider, ProviderCapabilities  # noqa: E402
from linch.sessions import InMemorySessionStore  # noqa: E402
from linch.tools import ToolContext, ToolRegistry, ToolResult  # noqa: E402
from linch.types import (  # noqa: E402
    Message,
    ProviderRequest,
    SystemBlock,
    TextBlock,
    Usage,
    block_to_dict,
)


@dataclass(slots=True)
class CacheCall:
    index: int
    prompt_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    wire_chars: int
    read_chars: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "prompt_tokens": self.prompt_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "wire_chars": self.wire_chars,
            "read_chars": self.read_chars,
        }


@dataclass(slots=True)
class ScenarioResult:
    name: str
    description: str
    provider_calls: int
    prompt_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cache_read_ratio: float
    calls: list[CacheCall] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "provider_calls": self.provider_calls,
            "prompt_tokens": self.prompt_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_ratio": self.cache_read_ratio,
            "calls": [call.to_dict() for call in self.calls],
        }


class PrefixCacheProbeProvider(BaseProvider):
    """Offline provider that simulates provider-side prefix cache reads/writes."""

    id = "prefix-cache-probe"

    def __init__(self, *, tool_turns: int) -> None:
        self.tool_turns = tool_turns
        self.calls: list[CacheCall] = []
        self._written_prefixes: list[str] = []

    def context_window(self, model: str) -> int:
        return 128_000

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(context_window=self.context_window(model), prompt_cache=True)

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]:
        call_index = len(self.calls) + 1
        wire = _wire_request(req)
        read_chars = max(
            (_common_prefix_len(wire, written) for written in self._written_prefixes),
            default=0,
        )
        prompt_tokens = _estimate_tokens(wire)
        cache_read_tokens = _estimate_tokens(wire[:read_chars]) if read_chars else 0
        cache_creation_tokens = max(0, prompt_tokens - cache_read_tokens)
        self._written_prefixes.append(wire)
        self.calls.append(
            CacheCall(
                index=call_index,
                prompt_tokens=prompt_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                wire_chars=len(wire),
                read_chars=read_chars,
            )
        )

        yield {"type": "message_start", "model": req.model}
        if call_index <= self.tool_turns:
            tool_name = str((req.tools[0] if req.tools else {"name": "LookupA"})["name"])
            tool_id = f"cache_probe_{call_index}"
            yield {"type": "tool_use_start", "id": tool_id, "name": tool_name}
            yield {
                "type": "tool_use_input_delta",
                "id": tool_id,
                "json_delta": json.dumps({"query": f"turn-{call_index}"}),
            }
            yield {"type": "tool_use_end", "id": tool_id}
            yield {
                "type": "message_end",
                "stop_reason": "tool_use",
                "usage": Usage(
                    input_tokens=prompt_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_creation_tokens=cache_creation_tokens,
                ),
                "provider_metadata": None,
            }
            return

        yield {"type": "text_delta", "text": "done"}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(
                input_tokens=prompt_tokens,
                output_tokens=1,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
            ),
            "provider_metadata": None,
        }


class BenchTool:
    description = "Deterministic benchmark lookup tool."
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    scope = "read"
    parallel = True

    def __init__(self, name: str) -> None:
        self.name = name

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return str(input.get("query", self.name))

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(content=f"{self.name}: {input.get('query', '')}", summary=self.name)


class DynamicMessageContext:
    async def build(self, turn: Any) -> ContextBuildResult:
        return ContextBuildResult(
            messages=[
                Message(
                    role="user",
                    content=[
                        TextBlock(
                            text=f"<volatile-context>turn={turn.turn_index}</volatile-context>"
                        )
                    ],
                )
            ],
            metadata={"scenario": "dynamic_message_context"},
        )


class DynamicSystemTail:
    async def build(self, turn: Any) -> ContextBuildResult:
        return ContextBuildResult(
            system_blocks=[
                SystemBlock(
                    text=f"Volatile policy tail for turn {turn.turn_index}.",
                    cacheable=False,
                )
            ],
            metadata={"scenario": "dynamic_system_tail"},
        )


class RotatingSelectedTools:
    async def build(self, turn: Any) -> ContextBuildResult:
        name = "LookupA" if turn.turn_index % 2 == 0 else "LookupB"
        return ContextBuildResult(
            selected_tools={name},
            metadata={"scenario": "rotating_selected_tools", "selected": name},
        )


def _wire_request(req: ProviderRequest) -> str:
    """Canonical logical request shape used by the offline prefix-cache probe."""

    payload = [
        ("tools", req.tools),
        (
            "system",
            [
                {
                    "text": block.text,
                    "cacheable": block.cacheable,
                }
                for block in req.system
            ],
        ),
        (
            "messages",
            [
                {
                    "role": message.role,
                    "content": [block_to_dict(block) for block in message.content],
                }
                for message in req.messages
            ],
        ),
        ("model", req.model),
        ("tool_choice", req.tool_choice),
        ("output_schema", req.output_schema.name if req.output_schema else None),
        ("cache_ttl", req.cache_ttl),
    ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _common_prefix_len(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    index = 0
    while index < limit and a[index] == b[index]:
        index += 1
    return index


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(BenchTool("LookupA"))
    registry.register(BenchTool("LookupB"))
    return registry


def _cache_read_ratio(prompt_tokens: int, cache_read_tokens: int) -> float:
    return round(cache_read_tokens / prompt_tokens, 4) if prompt_tokens else 0.0


async def _run_scenario(
    name: str,
    description: str,
    *,
    tool_turns: int,
    context_builder: Any = None,
) -> ScenarioResult:
    provider = PrefixCacheProbeProvider(tool_turns=tool_turns)
    hooks = [ContextInjectionHook(context_builder)] if context_builder is not None else []
    agent = Agent(
        model="cache-probe",
        provider=provider,
        tools=_tool_registry(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        hooks=hooks,
        features=FeatureFlags(skills=False, subagents=False, mcp=False, filesystem=False),
        result_offload=None,
    )
    session = await agent.session()
    async for _event in session.run("Use the lookup tool a few times, then answer."):
        pass

    prompt_tokens = sum(call.prompt_tokens for call in provider.calls)
    cache_read_tokens = sum(call.cache_read_tokens for call in provider.calls)
    cache_creation_tokens = sum(call.cache_creation_tokens for call in provider.calls)
    return ScenarioResult(
        name=name,
        description=description,
        provider_calls=len(provider.calls),
        prompt_tokens=prompt_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_ratio=_cache_read_ratio(prompt_tokens, cache_read_tokens),
        calls=list(provider.calls),
    )


async def run_prompt_cache_benchmark(*, tool_turns: int = 3) -> list[ScenarioResult]:
    scenarios = [
        (
            "stable_tools",
            "Stable tools and no ephemeral context; shows the best-case growing prefix.",
            None,
        ),
        (
            "dynamic_message_context",
            "Adds a fresh ephemeral context message on each provider call.",
            DynamicMessageContext(),
        ),
        (
            "dynamic_system_tail",
            "Adds a fresh cacheable=False system tail on each provider call.",
            DynamicSystemTail(),
        ),
        (
            "rotating_selected_tools",
            "Alternates selected_tools between provider calls; tools are first in the prefix.",
            RotatingSelectedTools(),
        ),
    ]
    results: list[ScenarioResult] = []
    for name, description, builder in scenarios:
        results.append(
            await _run_scenario(
                name,
                description,
                tool_turns=tool_turns,
                context_builder=builder,
            )
        )
    return results


def render_markdown(results: Sequence[ScenarioResult], *, tool_turns: int) -> str:
    lines = [
        "# Linch Prompt Cache Benchmark",
        "",
        "Offline prefix-cache simulation using a mock provider. This measures request-shape ",
        "cacheability inside one tool loop; it does not prove live provider cache behavior.",
        "",
        f"- tool_turns: {tool_turns}",
        "",
        "| Scenario | Calls | Prompt Tokens | Cache Read | Cache Write | Read Ratio |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result.name} | {result.provider_calls} | {result.prompt_tokens} | "
            f"{result.cache_read_tokens} | {result.cache_creation_tokens} | "
            f"{result.cache_read_ratio:.2%} |"
        )
    lines.extend(["", "## Per-call reads"])
    for result in results:
        reads = ", ".join(str(call.cache_read_tokens) for call in result.calls)
        lines.append(f"- {result.name}: {reads}")
    return "\n".join(lines)


def render_json(results: Sequence[ScenarioResult], *, tool_turns: int) -> str:
    return json.dumps(
        {
            "kind": "linch_prompt_cache_benchmark",
            "tool_turns": tool_turns,
            "note": (
                "Offline mock-provider prefix-cache simulation. Use live provider diagnostics "
                "to validate real server-side cache hits."
            ),
            "scenarios": [result.to_dict() for result in results],
        },
        indent=2,
        sort_keys=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark Linch prompt-cache request shape.")
    parser.add_argument(
        "--tool-turns",
        type=int,
        default=3,
        help="Number of tool-use provider calls before the final text call.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Output format.",
    )
    return parser


async def _main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.tool_turns < 1:
        raise SystemExit("--tool-turns must be at least 1")
    results = await run_prompt_cache_benchmark(tool_turns=args.tool_turns)
    if args.format == "json":
        print(render_json(results, tool_turns=args.tool_turns))
    else:
        print(render_markdown(results, tool_turns=args.tool_turns))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

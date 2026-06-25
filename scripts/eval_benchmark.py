from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Literal

from linch import Agent
from linch.evals import (
    EvalBenchmarkResult,
    EvalBenchmarkTarget,
    ScriptedProvider,
    load_eval_suite,
    load_scripted_turns,
    run_eval_benchmark,
)
from linch.sessions import InMemorySessionStore
from linch.tools.registry import empty_tools

OutputFormat = Literal["summary", "markdown", "json"]


def format_summary(result: EvalBenchmarkResult) -> str:
    lines = [
        f"Linch eval benchmark: {result.suite_name}",
        f"targets: {result.total_targets}",
        f"passed_targets: {result.passed_targets}",
    ]
    if result.scorer_names:
        lines.append(f"scorers: {', '.join(result.scorer_names)}")
    lines.append("")
    for target in result.targets:
        lines.extend(
            [
                target.name,
                f"- cases: {target.result.total}",
                f"- passed: {target.result.passed}",
                f"- pass_rate: {target.result.pass_rate:.2%}",
                f"- duration_ms: {target.duration_ms:.3f}",
            ]
        )
    return "\n".join(lines)


def render_result(
    result: EvalBenchmarkResult,
    *,
    output: OutputFormat,
    include_events: bool = False,
) -> str:
    if output == "json":
        return json.dumps(result.to_dict(include_events=include_events), indent=2, sort_keys=True)
    if output == "markdown":
        return result.to_markdown()
    return format_summary(result)


async def run_scripted_benchmark(
    suite_path: str | Path,
    scripted_targets: list[str],
    *,
    model: str = "scripted-eval",
) -> EvalBenchmarkResult:
    suite = load_eval_suite(suite_path)
    targets: list[EvalBenchmarkTarget] = []
    for target_spec in scripted_targets:
        name, path = _split_target_spec(target_spec)
        provider = ScriptedProvider(load_scripted_turns(path))
        agent = Agent(
            model=model,
            provider=provider,
            tools=empty_tools(),
            session_store=InMemorySessionStore(),
            permissions={"mode": "skip-dangerous"},
        )
        targets.append(EvalBenchmarkTarget(name=name, agent=agent))
    return await run_eval_benchmark(suite, targets)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a Linch eval suite against deterministic scripted targets."
    )
    parser.add_argument("suite", help="Path to a JSON/YAML eval suite.")
    parser.add_argument(
        "--scripted",
        action="append",
        required=True,
        metavar="NAME=PATH",
        help="ScriptedProvider turns file. Repeat to compare targets. NAME= is optional.",
    )
    parser.add_argument(
        "--model",
        default="scripted-eval",
        help="Model id to place on scripted Agent instances. Defaults to scripted-eval.",
    )
    parser.add_argument(
        "--format",
        choices=("summary", "markdown", "json"),
        default="summary",
        help="Output format. Defaults to summary.",
    )
    parser.add_argument(
        "--include-events",
        action="store_true",
        help="Include raw events in JSON output.",
    )
    parser.add_argument(
        "--output",
        help="Optional path to write the rendered report instead of stdout.",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=1.0,
        help="Minimum pass rate required for every target. Defaults to 1.0.",
    )
    return parser


async def _main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = await run_scripted_benchmark(args.suite, args.scripted, model=args.model)
    text = render_result(result, output=args.format, include_events=args.include_events)

    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)

    failed = any(target.result.pass_rate < args.fail_under for target in result.targets)
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(_main())


def _split_target_spec(value: str) -> tuple[str, str]:
    if "=" in value:
        name, path = value.split("=", 1)
        if name and path:
            return name, path
    path = value
    return Path(path).stem, path


if __name__ == "__main__":
    raise SystemExit(main())

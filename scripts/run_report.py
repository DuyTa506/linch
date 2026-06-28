from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Literal

from linch import RunReport, SqliteRunStore, load_run_report

OutputFormat = Literal["summary", "markdown", "json"]


def format_summary(report: RunReport) -> str:
    summary = report.summary
    usage = _dict(summary.get("usage"))
    tools = _dict(summary.get("tools"))
    context = _dict(summary.get("context"))
    recovery = _dict(summary.get("recovery"))
    risk = _dict(summary.get("risk"))
    slowest = _dict(tools.get("slowest_tool"))
    top_slowest = _list_of_dicts(tools.get("top_slowest"))
    top_failures = _list_of_dicts(tools.get("top_failures"))

    lines = [
        f"Linch run report: {report.run_id or '<unknown>'}",
        f"status: {report.status}",
        f"phase: {report.phase or '<none>'}",
        f"events: {report.event_count}",
        f"duration_ms: {summary.get('duration_ms')}",
        "",
        "Usage",
        f"- input_tokens: {usage.get('input_tokens', 0)}",
        f"- output_tokens: {usage.get('output_tokens', 0)}",
        f"- cache_read_tokens: {usage.get('cache_read_tokens', 0)}",
        f"- cache_creation_tokens: {usage.get('cache_creation_tokens', 0)}",
        f"- cache_read_ratio: {usage.get('cache_read_ratio', 0)}",
        f"- total_tokens: {usage.get('total_tokens', 0)}",
        f"- total_cost_usd: {usage.get('total_cost_usd')}",
        "",
        "Tools",
        f"- total: {tools.get('total', 0)}",
        f"- failed: {tools.get('failed', 0)}",
        f"- error_rate: {tools.get('error_rate', 0)}",
        f"- total_duration_ms: {tools.get('total_duration_ms', 0)}",
        f"- average_duration_ms: {tools.get('average_duration_ms', 0)}",
        f"- max_duration_ms: {tools.get('max_duration_ms', 0)}",
    ]
    if slowest:
        lines.append(
            "- slowest: {tool} ({duration}ms, error={error})".format(
                tool=slowest.get("tool_name", ""),
                duration=slowest.get("duration_ms", 0),
                error=slowest.get("is_error", False),
            )
        )
    if top_slowest:
        lines.append("- top slow:")
        for call in top_slowest:
            lines.append(
                "  - {tool} ({duration}ms, error={error}) {summary}".format(
                    tool=call.get("tool_name", ""),
                    duration=call.get("duration_ms", 0),
                    error=call.get("is_error", False),
                    summary=call.get("summary", ""),
                ).rstrip()
            )
    if top_failures:
        lines.append("- top failures:")
        for call in top_failures:
            detail = call.get("error") or call.get("result") or ""
            lines.append(
                "  - {tool} ({duration}ms) {summary}{detail}".format(
                    tool=call.get("tool_name", ""),
                    duration=call.get("duration_ms"),
                    summary=call.get("summary", ""),
                    detail=f": {detail}" if detail else "",
                ).rstrip()
            )

    lines.extend(
        [
            "",
            "Context",
            f"- builds: {context.get('builds', 0)}",
            f"- trimmed_builds: {context.get('trimmed_builds', 0)}",
            f"- max_used_tokens: {context.get('max_used_tokens')}",
            f"- max_tokens_seen: {context.get('max_tokens_seen')}",
            f"- max_utilization: {context.get('max_utilization')}",
            f"- pressure: {context.get('pressure', 'none')}",
            "",
            "Recovery",
            f"- compactions: {recovery.get('compactions', 0)}",
            f"- compaction_tokens_saved: {recovery.get('compaction_tokens_saved', 0)}",
            f"- model_fallbacks: {recovery.get('model_fallbacks', 0)}",
            f"- verification_retries: {recovery.get('verification_retries', 0)}",
            f"- hook_retries: {recovery.get('hook_retries', 0)}",
            f"- result_offloads: {recovery.get('result_offloads', 0)}",
            f"- offload_hit_rate: {recovery.get('offload_hit_rate', 0)}",
            "",
            "Risk",
            f"- permission_requests: {risk.get('permission_requests', 0)}",
            f"- loop_guards: {risk.get('loop_guards', 0)}",
            f"- errors: {risk.get('errors', 0)}",
            f"- recovery_hints: {risk.get('recovery_hints', 0)}",
        ]
    )
    return "\n".join(lines)


async def load_report(path: str | Path, run_id: str) -> RunReport:
    async with SqliteRunStore(path) as store:
        report = await load_run_report(store, run_id)
    return report


async def render_report(path: str | Path, run_id: str, *, output: OutputFormat) -> str:
    report = await load_report(path, run_id)
    if report.run_id == "":
        raise SystemExit(f"Run not found: {run_id}")
    if output == "markdown":
        return report.to_markdown()
    if output == "json":
        return json.dumps(report.to_dict(), indent=2, sort_keys=True)
    return format_summary(report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a Linch run report from a SQLite run store."
    )
    parser.add_argument("run_id", help="Run id to inspect.")
    parser.add_argument(
        "--runs-db",
        default=".linch/runs.db",
        help="Path to the SqliteRunStore database. Defaults to .linch/runs.db.",
    )
    parser.add_argument(
        "--format",
        choices=("summary", "markdown", "json"),
        default="summary",
        help="Output format. Defaults to summary.",
    )
    return parser


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


async def _main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    text = await render_report(args.runs_db, args.run_id, output=args.format)
    print(text)
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())

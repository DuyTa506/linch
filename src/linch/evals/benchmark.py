"""Benchmark helpers for running eval suites against one or more agents."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..types import Usage
from .harness import EvalCase, EvalResult, run_eval
from .scorers import (
    context_metadata_contains,
    context_not_trimmed,
    context_selected_tool,
    cost_under,
    memory_recalled,
    recovery_succeeded,
    run_completed,
    schema_valid,
    text_contains,
    tool_called,
)
from .scripted import TextTurn, ToolUseTurn, Turn

Scorer = Callable[..., bool | None]


@dataclass(slots=True)
class EvalSuite:
    """A named collection of eval cases and scorers."""

    name: str
    cases: list[EvalCase]
    scorers: list[Scorer] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def scorer_names(self) -> list[str]:
        return [getattr(scorer, "__name__", repr(scorer)) for scorer in self.scorers]


@dataclass(slots=True)
class EvalBenchmarkTarget:
    """One agent configuration to run against an eval suite."""

    name: str
    agent: Any


@dataclass(slots=True)
class EvalTargetResult:
    """Result for one benchmark target."""

    name: str
    result: EvalResult
    duration_ms: float

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        return {
            "name": self.name,
            "duration_ms": round(self.duration_ms, 3),
            "total": self.result.total,
            "passed": self.result.passed,
            "pass_rate": self.result.pass_rate,
            "result": self.result.to_dict(include_events=include_events),
        }


@dataclass(slots=True)
class EvalBenchmarkResult:
    """Aggregated benchmark result across all targets."""

    suite_name: str
    targets: list[EvalTargetResult]
    scorer_names: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_targets(self) -> int:
        return len(self.targets)

    @property
    def passed_targets(self) -> int:
        return sum(1 for target in self.targets if target.result.passed == target.result.total)

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        return {
            "suite": self.suite_name,
            "scorers": list(self.scorer_names),
            "metadata": dict(self.metadata),
            "total_targets": self.total_targets,
            "passed_targets": self.passed_targets,
            "targets": [target.to_dict(include_events=include_events) for target in self.targets],
        }

    def to_markdown(self) -> str:
        lines = [
            f"# Linch Eval Benchmark: {self.suite_name}",
            "",
            f"- targets: {self.total_targets}",
            f"- passed_targets: {self.passed_targets}",
        ]
        if self.scorer_names:
            lines.append(f"- scorers: {', '.join(self.scorer_names)}")
        lines.extend(
            [
                "",
                "| Target | Cases | Passed | Pass Rate | Duration ms |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for target in self.targets:
            lines.append(
                f"| {target.name} | {target.result.total} | {target.result.passed} | "
                f"{target.result.pass_rate:.2%} | {target.duration_ms:.3f} |"
            )
        for target in self.targets:
            lines.extend(["", f"## {target.name}", "", target.result.to_markdown()])
        return "\n".join(lines)


async def run_eval_benchmark(
    suite: EvalSuite,
    targets: Sequence[EvalBenchmarkTarget],
) -> EvalBenchmarkResult:
    """Run *suite* against each target and capture pass rates and wall time."""

    results: list[EvalTargetResult] = []
    for target in targets:
        started = time.perf_counter()
        result = await run_eval(target.agent, suite.cases, suite.scorers)
        duration_ms = (time.perf_counter() - started) * 1000
        results.append(
            EvalTargetResult(
                name=target.name,
                result=result,
                duration_ms=duration_ms,
            )
        )
    return EvalBenchmarkResult(
        suite_name=suite.name,
        targets=results,
        scorer_names=suite.scorer_names,
        metadata=suite.metadata,
    )


def load_eval_suite(path: str | Path) -> EvalSuite:
    """Load an eval suite from a JSON or YAML file."""

    raw = _load_document(Path(path))
    data = _mapping(raw, "suite")

    name = _optional_string(data.get("name")) or Path(path).stem
    cases = [
        _case_from_spec(item, idx) for idx, item in enumerate(_list(data.get("cases"), "cases"))
    ]
    scorers = [_scorer_from_spec(item, idx) for idx, item in enumerate(data.get("scorers") or [])]
    metadata = dict(_mapping(data.get("metadata") or {}, "metadata"))
    return EvalSuite(name=name, cases=cases, scorers=scorers, metadata=metadata)


def load_scripted_turns(path: str | Path) -> list[Turn]:
    """Load ``ScriptedProvider`` turns from a JSON or YAML file."""

    raw = _load_document(Path(path))
    if isinstance(raw, Mapping):
        raw_turns = raw.get("turns")
    else:
        raw_turns = raw
    return [_turn_from_spec(item, idx) for idx, item in enumerate(_list(raw_turns, "turns"))]


def _load_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml  # type: ignore[reportMissingModuleSource]

        return yaml.safe_load(text)
    return json.loads(text)


def _case_from_spec(raw: Any, idx: int) -> EvalCase:
    data = _mapping(raw, f"cases[{idx}]")
    prompt = _required_string(data.get("prompt"), f"cases[{idx}].prompt")
    expected = _optional_string(data.get("expected")) or ""
    metadata = dict(_mapping(data.get("metadata") or {}, f"cases[{idx}].metadata"))
    return EvalCase(prompt=prompt, expected=expected, metadata=metadata)


def _scorer_from_spec(raw: Any, idx: int) -> Scorer:
    data = _mapping(raw, f"scorers[{idx}]")
    kind = _required_string(data.get("type"), f"scorers[{idx}].type")
    if kind == "text_contains":
        value = data.get("substring", data.get("text", data.get("contains", "{expected}")))
        return text_contains(_required_string(value, f"scorers[{idx}].substring"))
    if kind == "tool_called":
        return tool_called(_required_string(data.get("tool"), f"scorers[{idx}].tool"))
    if kind == "schema_valid":
        return schema_valid(dict(_mapping(data.get("schema"), f"scorers[{idx}].schema")))
    if kind == "cost_under":
        return cost_under(_required_float(data.get("budget_usd"), f"scorers[{idx}].budget_usd"))
    if kind == "context_selected_tool":
        return context_selected_tool(_required_string(data.get("tool"), f"scorers[{idx}].tool"))
    if kind == "context_not_trimmed":
        return context_not_trimmed()
    if kind == "context_metadata_contains":
        key = _required_string(data.get("key"), f"scorers[{idx}].key")
        return context_metadata_contains(key, data.get("expected"))
    if kind == "memory_recalled":
        ids = data.get("ids", data.get("id"))
        if isinstance(ids, str):
            return memory_recalled(ids)
        return memory_recalled(
            [
                _required_string(item, f"scorers[{idx}].ids[]")
                for item in _list(ids, f"scorers[{idx}].ids")
            ]
        )
    if kind == "recovery_succeeded":
        tool = data.get("tool")
        return recovery_succeeded(_optional_string(tool) if tool is not None else None)
    if kind == "run_completed":
        return run_completed()
    raise ValueError(f"Unknown scorer type at scorers[{idx}]: {kind}")


def _turn_from_spec(raw: Any, idx: int) -> Turn:
    data = _mapping(raw, f"turns[{idx}]")
    kind = _required_string(data.get("type"), f"turns[{idx}].type")
    usage = _usage_from_spec(data.get("usage"))
    if kind == "text":
        return TextTurn(
            text=_required_string(data.get("text"), f"turns[{idx}].text"),
            stop_reason=_optional_string(data.get("stop_reason")) or "end_turn",
            usage=usage,
        )
    if kind == "tool_use":
        return ToolUseTurn(
            tool_name=_required_string(data.get("tool_name"), f"turns[{idx}].tool_name"),
            tool_input=dict(_mapping(data.get("tool_input") or {}, f"turns[{idx}].tool_input")),
            tool_id=_optional_string(data.get("tool_id")) or f"scripted_tool_{idx + 1}",
            usage=usage,
        )
    raise ValueError(f"Unknown scripted turn type at turns[{idx}]: {kind}")


def _usage_from_spec(raw: Any) -> Usage:
    if raw is None:
        return Usage()
    data = _mapping(raw, "usage")
    return Usage(
        input_tokens=_int(data.get("input_tokens")),
        output_tokens=_int(data.get("output_tokens")),
        cache_read_tokens=_int(data.get("cache_read_tokens")),
        cache_creation_tokens=_int(data.get("cache_creation_tokens")),
    )


def _mapping(raw: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} must be an object")
    return raw


def _list(raw: Any, label: str) -> list[Any]:
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list")
    return raw


def _required_string(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{label} must be a non-empty string")
    return raw


def _optional_string(raw: Any) -> str | None:
    return raw if isinstance(raw, str) else None


def _required_float(raw: Any, label: str) -> float:
    if not isinstance(raw, int | float):
        raise ValueError(f"{label} must be a number")
    return float(raw)


def _int(raw: Any) -> int:
    return int(raw) if isinstance(raw, int | float) else 0

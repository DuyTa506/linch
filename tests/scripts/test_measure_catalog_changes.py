from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "measure_catalog_changes.py"
_SPEC = importlib.util.spec_from_file_location("measure_catalog_changes", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
measure = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = measure
_SPEC.loader.exec_module(measure)


def _requests(provider: str, scenario: str):
    return measure.build_requests(provider, scenario, "m", "nonce", 5)


@pytest.mark.parametrize("provider", sorted(measure.SCENARIOS))
@pytest.mark.parametrize("scenario", ["control", "tail_text"])
def test_tail_scenarios_leave_tools_identical(provider: str, scenario: str) -> None:
    seed, warm, measured = _requests(provider, scenario)
    assert seed == warm
    assert measured["tools"] == seed["tools"]


@pytest.mark.parametrize(
    ("provider", "scenario"),
    [
        ("anthropic", "native_add"),
        ("anthropic", "native_edit"),
        ("anthropic", "native_remove"),
        ("responses", "native_add"),
        ("responses", "native_remove"),
    ],
)
def test_native_changes_never_touch_tools(provider: str, scenario: str) -> None:
    seed, _, measured = _requests(provider, scenario)
    assert measured["tools"] == seed["tools"]


@pytest.mark.parametrize(
    ("scenario", "index"), [("prefix_append", 12), ("prefix_middle", 6), ("prefix_edit", 6)]
)
def test_prefix_changes_diverge_where_expected(scenario: str, index: int) -> None:
    seed, _, measured = _requests("chat", scenario)
    assert measured["tools"][:index] == seed["tools"][:index]
    assert measured["tools"] != seed["tools"]


def test_native_add_messages_match_documented_shapes() -> None:
    anthropic = _requests("anthropic", "native_add")[-1]["messages"][-1]
    assert anthropic["role"] == "system"
    assert anthropic["content"][0]["type"] == "tool_addition"
    responses = _requests("responses", "native_add")[-1]["input"][-1]
    assert (responses["type"], responses["role"]) == ("additional_tools", "developer")


def test_anthropic_system_message_ends_the_conversation() -> None:
    messages = _requests("anthropic", "tail_text")[-1]["messages"]
    assert [message["role"] for message in messages[-2:]] == ["user", "system"]


@pytest.mark.parametrize(
    ("provider", "body", "expected"),
    [
        (
            "anthropic",
            {
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 80,
                    "cache_creation_input_tokens": 10,
                }
            },
            (80, 100),
        ),
        (
            "responses",
            {"usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 80}}},
            (80, 100),
        ),
        (
            "chat",
            {"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}}},
            (80, 100),
        ),
        (
            "chat",
            {
                "usage": {"prompt_tokens": 20, "prompt_tokens_details": {"cached_tokens": 0}},
                "timings": {"cache_n": 80, "prompt_n": 20},
            },
            (80, 100),
        ),
    ],
)
def test_cached_tokens_reads_each_usage_shape(provider, body, expected) -> None:
    assert measure.cached_tokens(provider, body) == expected


def test_payloads_are_json_serializable() -> None:
    for provider, scenarios in measure.SCENARIOS.items():
        for scenario in scenarios:
            json.dumps(_requests(provider, scenario))

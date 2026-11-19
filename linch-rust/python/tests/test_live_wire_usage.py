"""The live suites read a server's reasoning-token count from its raw usage object.

vLLM reports it at `usage.completion_tokens_details.reasoning_tokens`; SGLang at
`usage.reasoning_tokens` and leaves `completion_tokens_details` null. The payloads are
trimmed from recorded live runs (no prompt text, no keys).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def load_helper() -> ModuleType:
    path = Path(__file__).parent / "live" / "wire_usage.py"
    spec = importlib.util.spec_from_file_location("wire_usage", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SGLANG = {
    "prompt_tokens": 307,
    "total_tokens": 334,
    "completion_tokens": 27,
    "prompt_tokens_details": {"cached_tokens": 0},
    "reasoning_tokens": 0,
    "completion_tokens_details": None,
}
SGLANG_THINKING = {**SGLANG, "completion_tokens": 250, "reasoning_tokens": 248}
VLLM = {
    "prompt_tokens": 36,
    "total_tokens": 66,
    "completion_tokens": 30,
    "completion_tokens_details": {"reasoning_tokens": 28},
}


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (SGLANG, 0),
        (SGLANG_THINKING, 248),
        (VLLM, 28),
        ({"completion_tokens": 3}, None),
        (None, None),
    ],
)
def test_reasoning_tokens_from_either_server_shape(
    usage: dict[str, Any] | None, expected: int | None
) -> None:
    assert load_helper().reasoning_tokens(usage) == expected

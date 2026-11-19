"""The live suites read a server's reasoning-token count from its raw usage object.

vLLM reports it at `usage.completion_tokens_details.reasoning_tokens`; SGLang at
`usage.reasoning_tokens` and leaves `completion_tokens_details` null when the turn did not
reason. `SGLANG` is the usage object of a recorded SGLang run (a one-word reply, thinking off).
The two thinking payloads are constructed from the recorded effort run (`completion_tokens` 217,
`reasoning_tokens` 213, 212 thinking deltas). That dump read the count from
`completion_tokens_details`, so SGLang fills the details when it reasons; whether it also fills the
top-level field then is not recorded, so both placements are covered.
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
SGLANG_THINKING = {**SGLANG, "completion_tokens": 217, "reasoning_tokens": 213}
SGLANG_THINKING_DETAILS = {
    **SGLANG,
    "completion_tokens": 217,
    "completion_tokens_details": {"reasoning_tokens": 213},
}
VLLM = {
    "prompt_tokens": 36,
    "total_tokens": 253,
    "completion_tokens": 217,
    "completion_tokens_details": {"reasoning_tokens": 213},
}


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (SGLANG, 0),
        (SGLANG_THINKING, 213),
        (SGLANG_THINKING_DETAILS, 213),
        (VLLM, 213),
        ({"completion_tokens": 3}, None),
        (None, None),
    ],
)
def test_reasoning_tokens_from_either_server_shape(
    usage: dict[str, Any] | None, expected: int | None
) -> None:
    assert load_helper().reasoning_tokens(usage) == expected

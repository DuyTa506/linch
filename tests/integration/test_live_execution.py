"""Live execution acceptance against DeepSeek; skipped without DEEPSEEK_API_KEY."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.acceptance.test_execution_parity import _case, _run

KEY = os.environ.get("DEEPSEEK_API_KEY", "")
pytestmark = pytest.mark.skipif(not KEY, reason="DEEPSEEK_API_KEY not set")


def _model(_turns: list, _prompt: str) -> dict:
    from linch.providers.openai_chat import OpenAIChatCompletionsProvider, OpenAIChatProviderOptions

    options = OpenAIChatProviderOptions(api_key=KEY, base_url="https://api.deepseek.com")
    return {"provider": OpenAIChatCompletionsProvider(options), "model": "deepseek-chat"}


@pytest.mark.parametrize("case", ["edit", "long", "stdin"])
async def test_real_model_completes_execution_task(case: str, tmp_path: Path) -> None:
    _, prompt, tools = _case(case, tmp_path)
    events = await _run(tmp_path, _model, [], prompt, tools, permissions={"mode": "skip-dangerous"})
    assert events[-1].subtype == "success"
    if case == "edit":
        assert "a+b" in (tmp_path / "calc.py").read_text().replace(" ", "")

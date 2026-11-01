"""Verify optional imports remain lazy at the package boundary."""

from __future__ import annotations

import subprocess
import sys

import pytest


def _fresh_import(body: str) -> str:
    """Run body in a fresh interpreter without test-suite modules."""
    return subprocess.run(
        [sys.executable, "-c", body], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_cold_import_does_not_pull_mcp_or_transports() -> None:
    out = _fresh_import(
        "import sys, linch\n"
        "_ = (linch.Agent, linch.Message, linch.Session, linch.ToolContext)\n"
        "_ = linch.InMemorySessionStore\n"
        "heavy = {m.split('.')[0] for m in sys.modules}\n"
        "offenders = sorted(heavy & {'mcp', 'uvicorn', 'starlette', 'fastapi'})\n"
        "print(offenders)\n"
    )
    assert out == "[]", f"cold import pulled heavy deps: {out}"


def test_cold_import_does_not_pull_provider_sdks() -> None:
    out = _fresh_import(
        "import sys, linch\n"
        "_ = (linch.AnthropicProvider, linch.OpenAIChatCompletionsProvider, linch.GeminiProvider)\n"
        "sdks = {'anthropic', 'openai', 'google.genai', 'asyncpg'}\n"
        "offenders = sorted(m for m in sdks if m in sys.modules)\n"
        "print(offenders)\n"
    )
    assert out == "[]", f"referencing provider classes pulled their SDKs: {out}"


def test_mcp_name_loads_mcp_on_demand() -> None:
    out = _fresh_import(
        "import sys, linch\n"
        "before = 'mcp' in sys.modules\n"
        "_ = linch.connect_mcp_servers\n"
        "after = 'mcp' in sys.modules\n"
        "print(f'{before},{after}')\n"
    )
    assert out == "False,True", out


def test_session_store_types_exported_top_level() -> None:
    import linch
    from linch.sessions import InMemorySessionStore

    assert linch.InMemorySessionStore is InMemorySessionStore


def test_lazy_access_caches_into_module_dict() -> None:
    out = _fresh_import(
        "import linch\n"
        "assert 'Agent' not in linch.__dict__\n"
        "_ = linch.Agent\n"
        "assert 'Agent' in linch.__dict__\n"
        "print('ok')\n"
    )
    assert out == "ok"


def test_unknown_attribute_raises_attribute_error() -> None:
    import linch

    with pytest.raises(AttributeError):
        linch.NoSuchName  # noqa: B018


def test_dir_lists_all_public_names() -> None:
    import linch

    assert dir(linch) == sorted(linch.__all__)

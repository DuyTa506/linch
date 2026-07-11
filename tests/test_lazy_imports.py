"""Lazy public exports (ROADMAP Phase 3.2).

`import linch` resolves `linch.__all__` names lazily (PEP 562), so importing the
package and touching core types must not drag in optional/heavy integrations
(MCP + its Starlette/Uvicorn transports, provider SDKs). Heavy modules load only
when a name that needs them is actually accessed.
"""

from __future__ import annotations

import subprocess
import sys


def _fresh_import(body: str) -> str:
    """Run `body` in a fresh interpreter (no test-suite modules preloaded)."""
    result = subprocess.run(
        [sys.executable, "-c", body],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_cold_import_does_not_pull_mcp_or_transports() -> None:
    out = _fresh_import(
        "import sys, linch\n"
        "_ = (linch.Agent, linch.Message, linch.Session, linch.ToolContext)\n"
        "_ = (linch.InMemorySessionStore, linch.SqliteSessionStore)\n"
        "heavy = {m.split('.')[0] for m in sys.modules}\n"
        "offenders = sorted(heavy & {'mcp', 'uvicorn', 'starlette', 'fastapi'})\n"
        "print(offenders)\n"
    )
    assert out == "[]", f"cold import pulled heavy deps: {out}"


def test_cold_import_does_not_pull_provider_sdks() -> None:
    out = _fresh_import(
        "import sys, linch\n"
        "_ = (linch.AnthropicProvider, linch.OpenAIChatCompletionsProvider, linch.GeminiProvider)\n"
        # google.generativeai is the Gemini SDK; a bare 'google' namespace stub is benign.
        "sdks = {'anthropic', 'openai', 'google.generativeai', 'asyncpg'}\n"
        "offenders = sorted(m for m in sdks if m in sys.modules)\n"
        "print(offenders)\n"
    )
    # Provider *classes* resolve without importing their SDKs (SDKs load in stream()).
    assert out == "[]", f"referencing provider classes pulled their SDKs: {out}"


def test_mcp_name_loads_mcp_on_demand() -> None:
    out = _fresh_import(
        "import sys, linch\n"
        "before = 'mcp' in sys.modules\n"
        "_ = linch.connect_mcp_servers\n"
        "after = 'mcp' in sys.modules\n"
        "print(f'{before},{after}')\n"
    )
    # mcp package is installed in dev, so touching the name loads it; absent before.
    assert out == "False,True", out


def test_session_store_types_exported_top_level() -> None:
    import linch
    from linch.sessions import InMemorySessionStore, SqliteSessionStore

    assert linch.InMemorySessionStore is InMemorySessionStore
    assert linch.SqliteSessionStore is SqliteSessionStore


def test_lazy_access_caches_into_module_dict() -> None:
    out = _fresh_import(
        "import linch\n"
        "assert 'Agent' not in linch.__dict__  # not materialized until accessed\n"
        "_ = linch.Agent\n"
        "assert 'Agent' in linch.__dict__  # cached after first access\n"
        "print('ok')\n"
    )
    assert out == "ok"


def test_unknown_attribute_raises_attribute_error() -> None:
    import linch

    try:
        linch.NoSuchName  # noqa: B018
    except AttributeError:
        pass
    else:
        raise AssertionError("expected AttributeError for unknown top-level name")


def test_dir_lists_all_public_names() -> None:
    import linch

    assert dir(linch) == sorted(linch.__all__)

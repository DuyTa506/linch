"""Tests for tool-aware system blocks and SystemPromptConfig."""

from __future__ import annotations

import pytest


def _make_fake_tool(name: str):
    """Create a minimal duck-typed tool (no import-sensitive base class)."""

    class _FakeTool:
        def __init__(self, n):
            self.name = n
            self.description = f"Fake {n}"
            self.input_schema = {"type": "object", "properties": {}}
            self.scope = "read"
            self.parallel_safe = True

        def validate(self, raw):
            return raw

        async def execute(self, input, ctx):
            from linch.tools.base import ToolResult

            return ToolResult(content="ok", summary=self.name)

        def summarize(self, input):
            return self.name

    return _FakeTool(name)


# Convenience alias used by tests
def FakeTool(name):  # type: ignore[misc]
    return _make_fake_tool(name)


def _make_agent(tools=None, system_prompt_config=None, system_prompt=None, **kw):
    # All linch imports inside the function so tests survive test_hardening's sys.modules reset
    from linch import Agent
    from linch.providers.base import BaseProvider
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools
    from linch.types import Usage

    class FakeProvider(BaseProvider):
        id = "fake"

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "done"}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": Usage(),
                "provider_metadata": None,
            }

    return Agent(
        model="gpt-5",
        provider=FakeProvider(),
        tools=tools or empty_tools(_make_fake_tool("CustomTool")),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        system_prompt_config=system_prompt_config,
        system_prompt=system_prompt,
        **kw,
    )


def _block_texts(agent):
    return [b.text for b in agent.system_blocks]


# ── Tool-awareness ──────────────────────────────────────────────────────────


def test_retrieve_only_agent_no_swe_protocol():
    from linch.tools.registry import empty_tools

    agent = _make_agent(tools=empty_tools(FakeTool("RetrieveDocs")))
    combined = "\n".join(_block_texts(agent))
    assert "Edit" not in combined
    assert "Bash" not in combined
    assert "Glob" not in combined


def test_retrieve_only_agent_has_generic_parallel_hint():
    from linch.tools.registry import empty_tools

    agent = _make_agent(tools=empty_tools(FakeTool("RetrieveDocs")))
    combined = "\n".join(_block_texts(agent))
    assert "multiple tool calls" in combined


def test_default_swe_toolset_protocol_parity():
    from linch.tools.registry import tools_from_defaults

    agent = _make_agent(tools=tools_from_defaults())
    combined = "\n".join(_block_texts(agent))
    assert "Read a file before you Edit it" in combined
    assert "Bash runs in the user" in combined
    assert "Glob is for finding files" in combined
    assert "Grep is for" in combined
    assert "Prefer Edit over Write" in combined


def test_bash_only_no_edit_clause():
    from linch.tools.builtin import BashTool
    from linch.tools.registry import empty_tools

    r = empty_tools(BashTool())
    agent = _make_agent(tools=r)
    combined = "\n".join(_block_texts(agent))
    assert "Bash" in combined
    assert "Read a file before you Edit" not in combined


# ── SystemPromptConfig.replace_defaults ────────────────────────────────────


def test_replace_defaults_omits_identity_and_protocol():
    from linch.config import SystemPromptConfig

    cfg = SystemPromptConfig(replace_defaults=True, append="You are a SQL assistant.")
    agent = _make_agent(system_prompt_config=cfg)
    combined = "\n".join(_block_texts(agent))
    assert "autonomous software engineering assistant" not in combined
    assert "Tool use protocol" not in combined
    assert "SQL assistant" in combined


def test_replace_defaults_env_block_still_present():
    from linch.config import SystemPromptConfig

    cfg = SystemPromptConfig(replace_defaults=True)
    agent = _make_agent(system_prompt_config=cfg)
    combined = "\n".join(_block_texts(agent))
    assert "Working directory" in combined
    assert "Linch version" in combined


def test_custom_blocks_prepended_in_default_mode():
    from linch.config import SystemPromptConfig
    from linch.types import SystemBlock

    custom = SystemBlock(text="CUSTOM BLOCK", cacheable=True)
    cfg = SystemPromptConfig(blocks=[custom], replace_defaults=False)
    agent = _make_agent(system_prompt_config=cfg)
    texts = _block_texts(agent)
    custom_idx = next(i for i, t in enumerate(texts) if "CUSTOM BLOCK" in t)
    identity_idx = next(i for i, t in enumerate(texts) if "software engineering" in t)
    assert custom_idx < identity_idx


def test_system_prompt_sections_render_by_placement():
    from linch.config import SystemPromptConfig, SystemPromptSection

    cfg = SystemPromptConfig(
        sections=[
            SystemPromptSection(
                name="before",
                text="BEFORE DEFAULTS",
                placement="before_defaults",
            ),
            SystemPromptSection(
                name="after-defaults",
                text="AFTER DEFAULTS",
                placement="after_defaults",
                cacheable=False,
            ),
            SystemPromptSection(
                name="after-env",
                text="AFTER ENV",
                placement="after_env",
            ),
        ],
        append="APPEND",
    )
    agent = _make_agent(system_prompt_config=cfg)
    texts = _block_texts(agent)

    before_idx = texts.index("BEFORE DEFAULTS")
    identity_idx = next(i for i, t in enumerate(texts) if "software engineering" in t)
    after_defaults_idx = texts.index("AFTER DEFAULTS")
    env_idx = next(i for i, t in enumerate(texts) if t.startswith("Environment:"))
    after_env_idx = texts.index("AFTER ENV")
    append_idx = next(i for i, t in enumerate(texts) if "APPEND" in t)

    assert before_idx < identity_idx < after_defaults_idx < env_idx < after_env_idx < append_idx
    assert agent.system_blocks[after_defaults_idx].cacheable is False


def test_system_prompt_sections_replace_defaults_skip_identity():
    from linch.config import SystemPromptConfig, SystemPromptSection

    cfg = SystemPromptConfig(
        replace_defaults=True,
        sections=[
            SystemPromptSection(
                name="policy",
                text="DOMAIN POLICY",
                placement="after_defaults",
            )
        ],
    )
    agent = _make_agent(system_prompt_config=cfg)
    combined = "\n".join(_block_texts(agent))

    assert "DOMAIN POLICY" in combined
    assert "autonomous software engineering assistant" not in combined
    assert "Environment:" in combined


def test_system_prompt_sections_invalid_placement_raises():
    from linch.config import SystemPromptConfig, SystemPromptSection
    from linch.errors import ConfigError

    cfg = SystemPromptConfig(
        sections=[
            SystemPromptSection(
                name="bad",
                text="BAD",
                placement="wrong",  # type: ignore[arg-type]
            )
        ]
    )
    agent = _make_agent(system_prompt_config=cfg)

    with pytest.raises(ConfigError):
        _ = agent.system_blocks


def test_system_prompt_appended():
    agent = _make_agent(system_prompt="Always speak in haiku.")
    combined = "\n".join(_block_texts(agent))
    assert "Always speak in haiku." in combined


def test_system_prompt_config_append_overrides_system_prompt():
    from linch.config import SystemPromptConfig

    cfg = SystemPromptConfig(append="From config.")
    agent = _make_agent(system_prompt_config=cfg, system_prompt="From kwarg.")
    combined = "\n".join(_block_texts(agent))
    assert "From config." in combined


# ── Cache invalidation ──────────────────────────────────────────────────────


def test_refresh_invalidates_cache():
    agent = _make_agent()
    blocks1 = agent.system_blocks
    agent._refresh_system_blocks()
    blocks2 = agent.system_blocks
    assert blocks1 is not blocks2
    assert [b.text for b in blocks1] == [b.text for b in blocks2]


# ── Feature flags ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feature_flags_disable_skills_connect():
    from linch.config import FeatureFlags

    agent = _make_agent()
    agent.features = FeatureFlags(skills=False, subagents=False, mcp=False)
    session = await agent.session()
    assert session is not None
    assert agent.skills == {}

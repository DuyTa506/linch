from __future__ import annotations

from typing import Any, cast

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.tools import StubTool, tool_registry


def FakeTool(name: str) -> StubTool:  # type: ignore[misc]
    return StubTool(name, parallel=True)


def _make_agent(tools=None, system_prompt_config=None, system_prompt=None, **kw):
    return make_agent(
        ScriptedProvider([TextTurn("done")]),
        model="gpt-5",
        tools=tools or tool_registry(FakeTool("CustomTool")),
        system_prompt_config=system_prompt_config,
        system_prompt=system_prompt,
        **kw,
    )


def _block_texts(agent):
    return [b.text for b in agent.system_blocks]


def test_retrieve_only_agent_no_swe_protocol():
    agent = _make_agent(tools=tool_registry(FakeTool("RetrieveDocs")))
    combined = "\n".join(_block_texts(agent))
    assert "Edit" not in combined
    assert "Bash" not in combined
    assert "Glob" not in combined
    assert "multiple tool calls" in combined
    for phrase in ("software engineering", "codebase", "task orchestration", "Working directory"):
        assert phrase not in combined


def test_default_swe_toolset_protocol_parity():
    from linch.tools.builtin import BashTool
    from linch.tools.registry import workspace_tools

    agent = _make_agent(tools=workspace_tools())
    combined = "\n".join(_block_texts(agent))
    assert "Read a file before you Edit it" in combined
    assert "Bash runs in the user" in combined
    assert "Glob is for finding files" in combined
    assert "Grep is for" in combined
    assert "Prefer Edit over Write" in combined
    bash = "\n".join(_block_texts(_make_agent(tools=tool_registry(BashTool()))))
    assert "Bash" in bash and "Read a file before you Edit" not in bash


def test_replace_defaults_omits_identity_and_protocol():
    from linch.config import SystemPromptConfig, SystemPromptSection

    cfg = SystemPromptConfig(
        replace_defaults=True,
        append="You are a SQL assistant.",
        sections=[SystemPromptSection(name="policy", text="DOMAIN POLICY")],
    )
    agent = _make_agent(system_prompt_config=cfg)
    combined = "\n".join(_block_texts(agent))
    assert "autonomous software engineering assistant" not in combined
    assert "Tool use protocol" not in combined
    assert "SQL assistant" in combined
    assert "DOMAIN POLICY" in combined
    assert "Runtime:" in combined
    assert "Linch version" in combined
    assert "Working directory" not in combined


def test_custom_blocks_prepended_in_default_mode():
    from linch.config import SystemPromptConfig, SystemPromptSection
    from linch.types import SystemBlock

    custom = SystemBlock(text="CUSTOM BLOCK", cacheable=True)
    cfg = SystemPromptConfig(
        blocks=[custom],
        sections=[
            SystemPromptSection(name="before", text="BEFORE", placement="before_defaults"),
            SystemPromptSection(name="after", text="AFTER", placement="after_env"),
        ],
        append="APPEND",
    )
    agent = _make_agent(system_prompt_config=cfg)
    texts = _block_texts(agent)
    custom_idx = next(i for i, t in enumerate(texts) if "CUSTOM BLOCK" in t)
    identity_idx = next(i for i, t in enumerate(texts) if "configured through the Linch SDK" in t)
    assert custom_idx < identity_idx
    append_idx = next(i for i, t in enumerate(texts) if "APPEND" in t)
    assert texts.index("BEFORE") < identity_idx < texts.index("AFTER") < append_idx


def test_system_prompt_sections_render_by_placement():
    from linch.config import SystemPromptConfig, SystemPromptSection

    cfg = SystemPromptConfig(
        sections=[
            SystemPromptSection(name="before", text="BEFORE DEFAULTS", placement="before_defaults"),
            SystemPromptSection(
                name="after-defaults",
                text="AFTER DEFAULTS",
                placement="after_defaults",
                cacheable=False,
            ),
            SystemPromptSection(name="after-env", text="AFTER ENV", placement="after_env"),
        ],
        append="APPEND",
    )
    agent = _make_agent(system_prompt_config=cfg)
    texts = _block_texts(agent)
    before = texts.index("BEFORE DEFAULTS")
    identity = next(i for i, t in enumerate(texts) if "configured through the Linch SDK" in t)
    after_defaults = texts.index("AFTER DEFAULTS")
    env = next(i for i, t in enumerate(texts) if t.startswith("Runtime:"))
    after_env = texts.index("AFTER ENV")
    append = next(i for i, t in enumerate(texts) if "APPEND" in t)
    assert before < identity < after_defaults < env < after_env < append
    assert agent.system_blocks[after_defaults].cacheable is False


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


def test_system_prompt_config_append_overrides_system_prompt():
    from linch.config import SystemPromptConfig

    cfg = SystemPromptConfig(append="From config.")
    agent = _make_agent(system_prompt_config=cfg, system_prompt="From kwarg.")
    combined = "\n".join(_block_texts(agent))
    assert "From config." in combined
    assert "From kwarg." not in combined

    direct = _make_agent(system_prompt="Always speak in haiku.")
    assert "Always speak in haiku." in "\n".join(_block_texts(direct))


def test_refresh_invalidates_cache():
    agent = _make_agent()
    blocks1 = agent.system_blocks
    agent._refresh_system_blocks()
    blocks2 = agent.system_blocks
    assert blocks1 is not blocks2
    assert [b.text for b in blocks1] == [b.text for b in blocks2]
    agent.tools.replace(cast(Any, FakeTool("CustomTool")))
    assert agent.system_blocks is not blocks2


def test_tool_prompt_section_must_use_typed_section():
    from linch.errors import ConfigError

    tool = FakeTool("Malformed")
    cast(Any, tool).system_prompt_sections = [{"name": "bad", "text": "BAD"}]
    agent = _make_agent(tools=tool_registry(tool))

    with pytest.raises(ConfigError, match="SystemPromptSection"):
        _ = agent.system_blocks


def test_tool_prompt_sections_are_sorted_and_filtered_by_active_tool():
    from linch.config import SystemPromptSection

    zulu = FakeTool("Zulu")
    cast(Any, zulu).system_prompt_sections = [
        SystemPromptSection(name="zulu-policy", text="ZULU POLICY", placement="after_defaults")
    ]
    alpha = FakeTool("Alpha")
    cast(Any, alpha).system_prompt_sections = [
        SystemPromptSection(name="alpha-policy", text="ALPHA POLICY", placement="after_defaults")
    ]
    agent = _make_agent(tools=tool_registry(zulu, alpha))
    texts = _block_texts(agent)
    assert texts.index("ALPHA POLICY") < texts.index("ZULU POLICY")
    filtered = [block.text for block in agent.build_system_blocks_for_tool_names(["Zulu"])]
    assert "ZULU POLICY" in filtered
    assert "ALPHA POLICY" not in filtered


def test_dynamic_tool_prompt_contribution_invalidates_agent_cache():
    from linch.config import SystemPromptSection

    class DynamicPromptTool(type(FakeTool("Base"))):
        policy = "POLICY V1"

        def system_prompt_sections(self):
            return [SystemPromptSection(name="dynamic", text=self.policy)]

    tool = DynamicPromptTool("Dynamic")
    alpha = FakeTool("Alpha")
    cast(Any, alpha).system_prompt_sections = [SystemPromptSection(name="alpha", text="ALPHA")]
    agent = _make_agent(tools=tool_registry(tool, alpha))
    blocks1 = agent.system_blocks
    tool.policy = "POLICY V2"
    blocks2 = agent.system_blocks

    assert blocks1 is not blocks2
    assert "POLICY V1" in [block.text for block in blocks1]
    assert "POLICY V2" in [block.text for block in blocks2]
    assert "ALPHA" in [block.text for block in blocks2]
    assert "ALPHA" not in [
        block.text for block in agent.build_system_blocks_for_tool_names(["Dynamic"])
    ]


def test_custom_tool_agent_prompt_is_domain_neutral():
    agent = _make_agent(tools=tool_registry(FakeTool("RetrieveInvoices")))
    combined = "\n".join(_block_texts(agent))
    assert "configured through the Linch SDK" in combined
    assert "RetrieveInvoices" in combined
    for phrase in ("software engineering", "codebase", "Bash", "Edit a file", "task orchestration"):
        assert phrase not in combined


@pytest.mark.asyncio
async def test_feature_flags_disable_skills_connect():
    from linch.config import FeatureFlags

    agent = _make_agent()
    agent.features = FeatureFlags(skills=False, subagents=False, mcp=False)
    session = await agent.session()
    assert session is not None
    assert agent.skills == {}


@pytest.mark.asyncio
async def test_bare_agent_defaults_are_inert_and_ephemeral(tmp_path):
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    agent = Agent(model="gpt-5", provider=_make_agent().provider, cwd=str(tmp_path))

    assert agent.tools.list() == []
    assert not agent.features.skills
    assert not agent.features.subagents
    assert not agent.features.mcp
    assert not agent.features.filesystem
    assert agent.session_store is None

    await agent.session()
    assert isinstance(agent.session_store, InMemorySessionStore)
    assert not (tmp_path / ".linch").exists()

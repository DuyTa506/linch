"""Tests for ToolRegistry extensibility: unregister, replace, copy, subset."""

from __future__ import annotations

import pytest

from linch.kernel import Disposable
from linch.tools.registry import (
    ToolRegistry,
    empty_tools,
    workspace_tools,
)
from tests.support.tools import StubTool

# ── Minimal fake tool ───────────────────────────────────────────────────────


class FakeTool(StubTool):
    def __init__(self, name: str, *, tags: tuple[str, ...] = ()) -> None:
        super().__init__(name, parallel=True)
        self.description = f"Fake {name}"
        self.tags = tags


# ── Tests ───────────────────────────────────────────────────────────────────


def test_unregister_removes_tool():
    r = workspace_tools()
    assert r.get("Bash") is not None
    removed = r.unregister("Bash")
    assert removed is not None
    assert removed.name == "Bash"
    assert r.get("Bash") is None


def test_unregister_missing_returns_none():
    r = ToolRegistry()
    assert r.unregister("NonExistent") is None


def test_replace_overwrites_existing():
    r = ToolRegistry()
    original = FakeTool("MyTool")
    r.register(original)
    replacement = FakeTool("MyTool")
    r.replace(replacement)
    assert r.get("MyTool") is replacement


def test_replace_registers_new_tool():
    r = ToolRegistry()
    tool = FakeTool("NewTool")
    r.replace(tool)  # should not raise even though name is not registered
    assert r.get("NewTool") is tool


def test_copy_is_independent():
    r = workspace_tools()
    c = r.copy()
    c.unregister("Bash")
    assert r.get("Bash") is not None  # original unchanged
    assert c.get("Bash") is None


def test_subset_include():
    r = workspace_tools()
    sub = r.subset(include={"Read", "Write"})
    names = {t.name for t in sub.list()}
    assert names == {"Read", "Write"}


def test_subset_exclude():
    r = workspace_tools()
    sub = r.subset(exclude={"Bash"})
    assert sub.get("Bash") is None
    assert sub.get("Read") is not None


def test_subset_include_and_exclude():
    r = workspace_tools()
    r.register(FakeTool("CustomA"))
    sub = r.subset(include={"Read", "Bash", "CustomA"}, exclude={"Bash"})
    names = {t.name for t in sub.list()}
    assert names == {"Read", "CustomA"}


def test_empty_tools_no_args():
    r = empty_tools()
    assert r.list() == []


def test_workspace_preset_is_explicit_full_registry():
    expected = {"Read", "Write", "Edit", "Bash", "Glob", "Grep"}

    assert expected <= {tool.name for tool in workspace_tools().list()}


def test_empty_tools_with_args():
    a = FakeTool("A")
    b = FakeTool("B")
    r = empty_tools(a, b)
    assert {t.name for t in r.list()} == {"A", "B"}


def test_workspace_tools_exclude():
    r = workspace_tools(exclude={"Bash", "Write"})
    assert r.get("Bash") is None
    assert r.get("Write") is None
    assert r.get("Read") is not None


def test_workspace_tools_extra():
    extra = FakeTool("MyTool")
    r = workspace_tools(extra=[extra])
    assert r.get("Bash") is not None
    assert r.get("MyTool") is extra


def test_register_duplicate_raises():
    r = ToolRegistry()
    r.register(FakeTool("X"))
    with pytest.raises(Exception, match="already registered"):
        r.register(FakeTool("X"))


@pytest.mark.parametrize("missing", ["summarize", "validate"])
def test_register_rejects_incomplete_tool(missing):
    incomplete = type("IncompleteTool", (FakeTool,), {missing: None})
    with pytest.raises(Exception, match=missing):
        ToolRegistry().register(incomplete("Incomplete"))


def test_replace_rejects_malformed_tool():
    class NoSummarizeTool(FakeTool):
        summarize = None  # type: ignore[assignment]

    r = ToolRegistry()
    with pytest.raises(Exception, match="summarize"):
        r.replace(NoSummarizeTool("Incomplete"))


def test_select_by_names_and_tags():
    r = ToolRegistry()
    search = FakeTool("SearchDocs", tags=("rag", "search"))
    calc = FakeTool("Calculator", tags=("math",))
    r.register(search)
    r.register(calc)

    selected = r.select(names={"Calculator"}, tags={"rag"})
    assert {tool.name for tool in selected.list()} == {"SearchDocs", "Calculator"}


def test_schemas_export_canonical_input_schema():
    class SchemaTool(FakeTool):
        configured_schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }

    r = ToolRegistry()
    tool = SchemaTool("SearchDocs")
    tool.input_schema = SchemaTool.configured_schema
    r.register(tool)

    assert r.schemas() == [
        {
            "name": "SearchDocs",
            "description": "Fake SearchDocs",
            "input_schema": SchemaTool.configured_schema,
        }
    ]


def test_registry_rejects_tool_with_only_legacy_schema_attribute():
    from linch.errors import ConfigError

    class LegacySchemaTool(FakeTool):
        schema = {"type": "object"}

        def __init__(self) -> None:
            super().__init__("LegacySchema")
            del self.input_schema

    with pytest.raises(ConfigError, match="input_schema"):
        ToolRegistry().register(LegacySchemaTool())


def test_registry_does_not_read_legacy_schema_when_exporting():
    class CanonicalSchemaTool(FakeTool):
        input_schema = {"type": "object"}

        @property
        def schema(self):
            raise AssertionError("legacy schema attribute was read")

    registry = ToolRegistry()
    registry.register(CanonicalSchemaTool("CanonicalSchema"))
    assert registry.schemas()[0]["input_schema"] == {"type": "object"}


@pytest.mark.asyncio
async def test_register_returns_disposer_that_removes_tool() -> None:
    reg = ToolRegistry()
    tool = StubTool("alpha")
    handle = reg.register(tool)
    assert isinstance(handle, Disposable)
    gen_after_register = reg.generation
    assert reg.get("alpha") is tool

    await handle.dispose()
    assert reg.get("alpha") is None
    assert reg.generation > gen_after_register


@pytest.mark.asyncio
async def test_dispose_is_idempotent() -> None:
    reg = ToolRegistry()
    handle = reg.register(StubTool("alpha"))
    await handle.dispose()
    gen = reg.generation
    await handle.dispose()
    assert reg.generation == gen
    assert reg.get("alpha") is None


@pytest.mark.asyncio
async def test_disposer_only_removes_its_own_instance() -> None:
    reg = ToolRegistry()
    handle = reg.register(StubTool("alpha"))
    replacement = StubTool("alpha")
    reg.replace(replacement)
    await handle.dispose()
    assert reg.get("alpha") is replacement


@pytest.mark.asyncio
async def test_stale_disposer_does_not_remove_reused_tool_instance() -> None:
    reg = ToolRegistry()
    tool = StubTool("alpha")
    stale = reg.register(tool)
    replacement = reg.replace(tool)

    await stale.dispose()
    assert reg.get("alpha") is tool
    await replacement.dispose()
    assert reg.get("alpha") is None


@pytest.mark.asyncio
async def test_replace_disposer_restores_previous_registration() -> None:
    reg = ToolRegistry()
    original = StubTool("alpha")
    reg.register(original)
    replacement = StubTool("alpha")
    handle = reg.replace(replacement)
    assert isinstance(handle, Disposable)

    await handle.dispose()
    assert reg.get("alpha") is original


@pytest.mark.asyncio
async def test_nested_replacements_unwind_in_lifo_order() -> None:
    reg = ToolRegistry()
    original = StubTool("alpha")
    first = StubTool("alpha")
    second = StubTool("alpha")
    original_handle = reg.register(original)
    first_handle = reg.replace(first)
    second_handle = reg.replace(second)

    await second_handle.dispose()
    assert reg.get("alpha") is first
    await first_handle.dispose()
    assert reg.get("alpha") is original
    await original_handle.dispose()
    assert reg.get("alpha") is None


@pytest.mark.asyncio
async def test_out_of_order_disposal_skips_disposed_predecessor() -> None:
    reg = ToolRegistry()
    original = StubTool("alpha")
    first = StubTool("alpha")
    second = StubTool("alpha")
    reg.register(original)
    first_handle = reg.replace(first)
    second_handle = reg.replace(second)

    generation = reg.generation
    await first_handle.dispose()
    assert reg.get("alpha") is second
    assert reg.generation == generation
    await second_handle.dispose()
    assert reg.get("alpha") is original
    assert reg.generation > generation


@pytest.mark.asyncio
async def test_unregister_breaks_replacement_restoration_chain() -> None:
    reg = ToolRegistry()
    original = StubTool("alpha")
    replacement = StubTool("alpha")
    reg.register(original)
    replacement_handle = reg.replace(replacement)

    assert reg.unregister("alpha") is replacement
    generation = reg.generation
    await replacement_handle.dispose()
    assert reg.get("alpha") is None
    assert reg.generation == generation

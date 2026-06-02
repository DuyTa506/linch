"""Tests for ToolRegistry extensibility: unregister, replace, copy, subset."""

from __future__ import annotations

import pytest

from linch.tools.base import ToolContext, ToolResult, ToolScope
from linch.tools.registry import ToolRegistry, default_tools, empty_tools, tools_from_defaults

# ── Minimal fake tool ───────────────────────────────────────────────────────


class FakeTool:
    def __init__(self, name: str, *, tags: tuple[str, ...] = ()) -> None:
        self.name = name
        self.description = f"Fake {name}"
        self.input_schema: dict = {"type": "object", "properties": {}}
        self.tags = tags
        self.scope: ToolScope = "read"
        self.parallel = True
        self.parallel_safe = True

    def validate(self, raw: dict) -> dict:
        return raw

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="ok", summary=self.name)

    def summarize(self, input: dict) -> str:
        return self.name


# ── Tests ───────────────────────────────────────────────────────────────────


def test_unregister_removes_tool():
    r = default_tools()
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
    r = default_tools()
    c = r.copy()
    c.unregister("Bash")
    assert r.get("Bash") is not None  # original unchanged
    assert c.get("Bash") is None


def test_subset_include():
    r = default_tools()
    sub = r.subset(include={"Read", "Write"})
    names = {t.name for t in sub.list()}
    assert names == {"Read", "Write"}


def test_subset_exclude():
    r = default_tools()
    sub = r.subset(exclude={"Bash"})
    assert sub.get("Bash") is None
    assert sub.get("Read") is not None


def test_subset_include_and_exclude():
    r = default_tools()
    r.register(FakeTool("CustomA"))
    sub = r.subset(include={"Read", "Bash", "CustomA"}, exclude={"Bash"})
    names = {t.name for t in sub.list()}
    assert names == {"Read", "CustomA"}


def test_empty_tools_no_args():
    r = empty_tools()
    assert r.list() == []


def test_empty_tools_with_args():
    a = FakeTool("A")
    b = FakeTool("B")
    r = empty_tools(a, b)
    assert {t.name for t in r.list()} == {"A", "B"}


def test_tools_from_defaults_exclude():
    r = tools_from_defaults(exclude={"Bash", "Write"})
    assert r.get("Bash") is None
    assert r.get("Write") is None
    assert r.get("Read") is not None


def test_tools_from_defaults_extra():
    extra = FakeTool("MyTool")
    r = tools_from_defaults(extra=[extra])
    assert r.get("Bash") is not None
    assert r.get("MyTool") is extra


def test_register_duplicate_raises():
    r = ToolRegistry()
    r.register(FakeTool("X"))
    with pytest.raises(Exception, match="already registered"):
        r.register(FakeTool("X"))


def test_add_and_remove_aliases():
    r = ToolRegistry()
    tool = FakeTool("RuntimeTool")
    r.add(tool)
    assert r.get("RuntimeTool") is tool
    assert r.remove("RuntimeTool") is tool
    assert r.get("RuntimeTool") is None


def test_select_by_names_and_tags():
    r = ToolRegistry()
    search = FakeTool("SearchDocs", tags=("rag", "search"))
    calc = FakeTool("Calculator", tags=("math",))
    r.add(search)
    r.add(calc)

    selected = r.select(names={"Calculator"}, tags={"rag"})
    assert {tool.name for tool in selected.list()} == {"SearchDocs", "Calculator"}


def test_schemas_accept_v2_schema_attribute():
    class SchemaTool(FakeTool):
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }

    r = ToolRegistry()
    r.add(SchemaTool("SearchDocs"))

    assert r.schemas() == [
        {
            "name": "SearchDocs",
            "description": "Fake SearchDocs",
            "input_schema": SchemaTool.schema,
        }
    ]

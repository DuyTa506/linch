"""Reversible tool registration (Phase 1): register() returns a Disposable."""

from __future__ import annotations

from typing import Any

import pytest

from linch.kernel import Disposable
from linch.tools import ToolRegistry
from linch.tools.base import ToolScope


class FakeTool:
    """Minimal duck-typed tool satisfying the registry's shape check."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "fake"
        self.input_schema: dict[str, Any] = {"type": "object", "properties": {}}
        self.scope: ToolScope = "read"
        self.parallel = False

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    async def execute(self, input: dict[str, Any], ctx: Any) -> str:
        return "ok"

    def summarize(self, input: dict[str, Any]) -> str:
        return "fake"


@pytest.mark.asyncio
async def test_register_returns_disposer_that_removes_tool() -> None:
    reg = ToolRegistry()
    tool = FakeTool("alpha")
    handle = reg.register(tool)
    assert isinstance(handle, Disposable)
    gen_after_register = reg.generation
    assert reg.get("alpha") is tool

    await handle.dispose()
    assert reg.get("alpha") is None
    assert reg.generation > gen_after_register  # removal bumps the structural revision


@pytest.mark.asyncio
async def test_dispose_is_idempotent() -> None:
    reg = ToolRegistry()
    handle = reg.register(FakeTool("alpha"))
    await handle.dispose()
    gen = reg.generation
    await handle.dispose()  # no-op
    assert reg.generation == gen
    assert reg.get("alpha") is None


@pytest.mark.asyncio
async def test_disposer_only_removes_its_own_instance() -> None:
    reg = ToolRegistry()
    handle = reg.register(FakeTool("alpha"))
    replacement = FakeTool("alpha")
    reg.replace(replacement)  # same name, different instance
    await handle.dispose()  # must NOT evict the newer occupant
    assert reg.get("alpha") is replacement


@pytest.mark.asyncio
async def test_stale_disposer_does_not_remove_reused_tool_instance() -> None:
    reg = ToolRegistry()
    tool = FakeTool("alpha")
    stale = reg.register(tool)
    replacement = reg.replace(tool)

    await stale.dispose()
    assert reg.get("alpha") is tool

    await replacement.dispose()
    assert reg.get("alpha") is None


@pytest.mark.asyncio
async def test_replace_disposer_restores_previous_registration() -> None:
    reg = ToolRegistry()
    original = FakeTool("alpha")
    reg.register(original)
    replacement = FakeTool("alpha")
    handle = reg.replace(replacement)
    assert isinstance(handle, Disposable)

    await handle.dispose()
    assert reg.get("alpha") is original


@pytest.mark.asyncio
async def test_nested_replacements_unwind_in_lifo_order() -> None:
    reg = ToolRegistry()
    original = FakeTool("alpha")
    first = FakeTool("alpha")
    second = FakeTool("alpha")
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
    original = FakeTool("alpha")
    first = FakeTool("alpha")
    second = FakeTool("alpha")
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
    original = FakeTool("alpha")
    replacement = FakeTool("alpha")
    reg.register(original)
    replacement_handle = reg.replace(replacement)

    assert reg.unregister("alpha") is replacement
    generation = reg.generation
    await replacement_handle.dispose()
    assert reg.get("alpha") is None
    assert reg.generation == generation


@pytest.mark.asyncio
async def test_add_returns_disposer() -> None:
    reg = ToolRegistry()
    handle = reg.add(FakeTool("alpha"))
    assert isinstance(handle, Disposable)
    await handle.dispose()
    assert reg.get("alpha") is None

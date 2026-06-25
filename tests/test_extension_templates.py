from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "examples" / "extensions"


def _load(name: str):
    path = TEMPLATES / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_provider_template_streams_normalized_events():
    from linch.types import Message, ProviderRequest, TextBlock

    module = _load("provider_template")
    provider = module.TemplateProvider()
    req = ProviderRequest(
        model="template-model",
        system=[],
        tools=[],
        messages=[Message(role="user", content=[TextBlock("hello")])],
    )

    events = [event async for event in provider.stream(req)]

    assert provider.context_window("template-model") > 0
    assert provider.capabilities("template-model").structured_output is False
    assert events[0]["type"] == "message_start"
    assert events[1] == {"type": "text_delta", "text": "Echo: hello"}
    assert events[2]["type"] == "message_end"


@pytest.mark.asyncio
async def test_memory_store_template_searches_and_filters():
    from linch.memory import MemoryItem

    module = _load("memory_store_template")
    store = module.TemplateMemoryStore()
    await store.upsert(
        [
            MemoryItem(
                id="m1",
                content="PTO rolls over each January",
                namespace="hr",
                metadata={"kind": "policy"},
            ),
            MemoryItem(id="m2", content="Deploy notes", namespace="eng"),
        ]
    )

    results = await store.search(
        "pto january",
        namespace="hr",
        metadata_filter={"kind": "policy"},
    )

    assert [result.item.id for result in results] == ["m1"]
    assert results[0].score == 1.0


@pytest.mark.asyncio
async def test_filesystem_backend_template_behaves_like_file_backend():
    module = _load("filesystem_backend_template")
    backend = module.TemplateFileBackend()

    await backend.write("notes/todo.txt", "one\ntwo\nthree")
    count = await backend.edit("/notes/todo.txt", "two", "TWO")

    assert count == 1
    assert await backend.exists("/notes/todo.txt") is True
    assert await backend.read("/notes/todo.txt", offset=2, limit=1) == "TWO"
    assert await backend.ls("/notes") == ["/notes/todo.txt"]
    await backend.delete("/notes/todo.txt")
    assert await backend.exists("/notes/todo.txt") is False


@pytest.mark.asyncio
async def test_tool_package_template_builds_registry_and_executes():
    from linch.tools import ToolContext

    module = _load("tool_package_template")
    registry = module.build_tools({"plan": "Pro"})
    tool = registry.get("TemplateLookup")
    advanced = registry.get("TemplateAdvancedLookup")
    assert tool is not None
    assert advanced is not None

    validated = tool.validate({"key": "plan"})
    result = await tool.execute(
        validated,
        ToolContext(cwd=".", session_id="s1", run_id="r1", session_store=None),
    )

    assert result.content == "Pro"
    assert tool.resources(validated)[0].resource == "template:plan"

    advanced_result = await advanced.execute(
        advanced.validate({"key": "plan"}),
        ToolContext(cwd=".", session_id="s1", run_id="r1", session_store=None),
    )
    assert advanced_result.summary == "lookup(plan)"


def test_hook_package_template_blocks_and_mutates():
    from linch.hooks import BeforeFinalAnswerContext, PreToolUseContext

    module = _load("hook_package_template")
    hook = module.TemplateAuditHook(blocked_tools={"Bash"})

    blocked = hook.on_pre_tool_use(
        PreToolUseContext(
            session=SimpleNamespace(id="s1"),
            run_id="r1",
            turn_index=0,
            tool_name="Bash",
        )
    )
    mutated = hook.on_before_final_answer(
        BeforeFinalAnswerContext(
            session=SimpleNamespace(id="s1"),
            run_id="r1",
            turn_index=0,
            final_text=" done ",
        )
    )

    assert blocked is not None
    assert blocked.action == "block"
    assert mutated is not None
    assert mutated.action == "mutate"
    assert mutated.final_text == "done"

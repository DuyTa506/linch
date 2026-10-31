from __future__ import annotations

import asyncio
import gc
import weakref

import pytest

from linch.errors import ConfigError
from linch.kernel import Context


class _Svc:
    def __init__(self, tag: str) -> None:
        self.tag = tag


@pytest.mark.asyncio
async def test_register_and_declared_access() -> None:
    ctx = Context()
    svc = _Svc("shell")
    ctx.register("execution", svc)
    assert ctx.execution is svc  # ctx.<name> declared access
    assert ctx.get("execution") is svc


@pytest.mark.asyncio
async def test_get_missing_returns_default() -> None:
    ctx = Context()
    assert ctx.get("nope") is None
    assert ctx.get("nope", "fallback") == "fallback"


@pytest.mark.asyncio
async def test_declared_access_missing_raises_attribute_error() -> None:
    ctx = Context()
    with pytest.raises(AttributeError):
        _ = ctx.execution


@pytest.mark.asyncio
async def test_double_register_same_scope_raises() -> None:
    ctx = Context()
    ctx.register("svc", _Svc("a"))
    with pytest.raises(ConfigError):
        ctx.register("svc", _Svc("b"))


@pytest.mark.asyncio
async def test_register_disposer_removes_service() -> None:
    ctx = Context()
    handle = ctx.register("svc", _Svc("a"))
    assert ctx.get("svc") is not None
    await handle.dispose()
    assert ctx.get("svc") is None


@pytest.mark.asyncio
async def test_service_change_event_fires_after_register_and_remove() -> None:
    ctx = Context()
    svc = _Svc("evented")
    seen: list[tuple[Context, str, object]] = []

    def changed(changed_ctx: Context, key: str) -> None:
        seen.append((changed_ctx, key, changed_ctx.get(key)))

    ctx.on("context/service", changed)
    handle = ctx.register("svc", svc)
    assert seen == [(ctx, "svc", svc)]

    await handle.dispose()
    assert seen == [(ctx, "svc", svc), (ctx, "svc", None)]


@pytest.mark.asyncio
async def test_service_remove_waits_for_async_change_listeners() -> None:
    ctx = Context()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = False

    async def changed(changed_ctx: Context, key: str) -> None:
        nonlocal finished
        if changed_ctx.get(key) is None:
            started.set()
            await release.wait()
            finished = True

    ctx.on("context/service", changed)
    handle = ctx.register("svc", _Svc("async"))
    disposing = asyncio.create_task(handle.dispose())
    await started.wait()
    assert not disposing.done()
    assert not finished

    release.set()
    await disposing
    assert finished


@pytest.mark.asyncio
async def test_service_remove_retries_failed_change_notification() -> None:
    ctx = Context()
    attempts = 0

    async def changed(changed_ctx: Context, key: str) -> None:
        nonlocal attempts
        if changed_ctx.get(key) is None:
            attempts += 1
            if attempts == 1:
                raise RuntimeError("listener failed")

    ctx.on("context/service", changed)
    handle = ctx.register("svc", _Svc("retry"))

    with pytest.raises(RuntimeError, match="listener failed"):
        await handle.dispose()
    assert ctx.get("svc") is None

    await handle.dispose()
    assert attempts == 2


@pytest.mark.asyncio
async def test_context_dispose_retries_failed_notification_after_listener_teardown() -> None:
    ctx = Context()
    calls = {"failed": 0, "ok": 0}

    async def failed(changed_ctx: Context, key: str) -> None:
        if changed_ctx.get(key) is None:
            calls["failed"] += 1
            if calls["failed"] == 1:
                raise RuntimeError("listener failed")

    async def ok(changed_ctx: Context, key: str) -> None:
        if changed_ctx.get(key) is None:
            calls["ok"] += 1

    ctx.on("context/service", failed)
    ctx.on("context/service", ok)
    ctx.register("svc", _Svc("context-retry"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="listener failed"):
        await ctx.dispose()
    assert calls == {"failed": 1, "ok": 1}

    await ctx.dispose()
    assert calls == {"failed": 2, "ok": 1}


@pytest.mark.asyncio
async def test_context_hides_services_while_disposal_is_running() -> None:
    ctx = Context()
    ctx.register("svc", _Svc("hidden"))
    started = asyncio.Event()
    release = asyncio.Event()

    async def teardown() -> None:
        started.set()
        await release.wait()

    ctx.effect(lambda: teardown)
    disposing = asyncio.create_task(ctx.dispose())
    await started.wait()
    assert ctx.get("svc") is None
    release.set()
    await disposing


@pytest.mark.asyncio
async def test_context_inbox_registrations_are_ordered_and_reversible() -> None:
    ctx = Context()

    async def first(_session: object, _run_id: str):
        if False:
            yield None

    async def second(_session: object, _run_id: str):
        if False:
            yield None

    first_handle = ctx.inbox(first)
    _second_handle = ctx.inbox(second)
    assert [producer for _token, producer in ctx._inbox or []] == [first, second]

    await first_handle.dispose()
    await first_handle.dispose()
    assert [producer for _token, producer in ctx._inbox or []] == [second]

    await ctx.dispose()
    assert ctx._inbox == []


@pytest.mark.asyncio
async def test_disposed_child_context_is_released_before_parent_disposal() -> None:
    parent = Context()
    child = parent.scope("temporary")
    reference = weakref.ref(child)

    await child.dispose()
    del child
    gc.collect()

    assert reference() is None
    await parent.dispose()


@pytest.mark.asyncio
async def test_scope_inherits_parent_services() -> None:
    parent = Context()
    parent.register("execution", _Svc("local"))
    child = parent.scope("agent-1")
    assert child.execution.tag == "local"  # inherited


@pytest.mark.asyncio
async def test_child_registration_invisible_to_parent() -> None:
    parent = Context()
    child = parent.scope("agent-1")
    child.register("agent_only", _Svc("x"))
    assert child.get("agent_only") is not None
    assert parent.get("agent_only") is None


@pytest.mark.asyncio
async def test_child_override_shadows_parent() -> None:
    parent = Context()
    parent.register("execution", _Svc("local"))
    child = parent.scope("agent-1")
    child.register("execution", _Svc("remote"))
    assert child.execution.tag == "remote"
    assert parent.execution.tag == "local"


@pytest.mark.asyncio
async def test_disposing_parent_disposes_child_scope() -> None:
    parent = Context()
    child = parent.scope("agent-1")
    removed: list[str] = []
    child.on("evt", lambda _p: None)
    child.effect(lambda: lambda: removed.append("child-effect"))
    await parent.dispose()
    assert removed == ["child-effect"]


@pytest.mark.asyncio
async def test_child_listeners_share_parent_bus_and_teardown_with_child() -> None:
    parent = Context()
    child = parent.scope("agent-1")
    seen: list[str] = []
    child.on("evt", lambda p: seen.append(f"child:{p}"))
    parent.events.emit("evt", "1")  # shared bus
    assert seen == ["child:1"]
    await child.dispose()
    parent.events.emit("evt", "2")
    assert seen == ["child:1"]  # child listener gone after child dispose


@pytest.mark.asyncio
async def test_two_contexts_are_isolated() -> None:
    a = Context()
    b = Context()
    a.register("svc", _Svc("a"))
    b.register("svc", _Svc("b"))
    assert a.execution if False else a.get("svc").tag == "a"
    assert b.get("svc").tag == "b"


@pytest.mark.asyncio
async def test_context_rejects_mutation_after_dispose() -> None:
    ctx = Context()
    await ctx.dispose()

    with pytest.raises(RuntimeError, match="disposing or disposed"):
        ctx.register("svc", _Svc("late"))
    with pytest.raises(RuntimeError, match="disposing or disposed"):
        ctx.on("evt", lambda: None)
    with pytest.raises(RuntimeError, match="disposing or disposed"):
        ctx.effect(lambda: None)
    with pytest.raises(RuntimeError, match="disposing or disposed"):
        ctx.scope("late")
    assert ctx.events.has_listeners("evt") is False


@pytest.mark.asyncio
async def test_context_rejects_mutation_while_disposing() -> None:
    ctx = Context()
    started = asyncio.Event()
    release = asyncio.Event()
    ctx.register("svc", _Svc("existing"))

    async def teardown() -> None:
        started.set()
        await release.wait()

    ctx.effect(lambda: teardown)
    disposing = asyncio.create_task(ctx.dispose())
    await started.wait()

    with pytest.raises(RuntimeError, match="disposing or disposed"):
        ctx.on("late", lambda: None)
    with pytest.raises(RuntimeError, match="disposing or disposed"):
        ctx.register("svc", _Svc("replacement"))
    assert ctx.events.has_listeners("late") is False

    release.set()
    await disposing


@pytest.mark.asyncio
async def test_context_dispose_continues_after_cleanup_failure() -> None:
    ctx = Context()
    seen: list[str] = []
    ctx.register("svc", _Svc("owned"))
    ctx.on("evt", lambda _payload: seen.append("listener"))

    def fail() -> None:
        raise RuntimeError("cleanup failed")

    ctx.effect(lambda: fail)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await ctx.dispose()

    assert ctx.get("svc") is None
    ctx.events.emit("evt", None)
    assert seen == []

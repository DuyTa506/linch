from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from linch.errors import ConfigError
from linch.kernel import Context, Disposable


async def _settle() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _agent(*, context: Context | None = None) -> Any:
    from linch import Agent
    from linch.evals import ScriptedProvider

    return Agent(
        model="m",
        provider=ScriptedProvider([]),
        context=context,
        result_offload=None,
    )


@dataclass
class _Plugin:
    name: str = "plugin"
    version: str = "1"
    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    calls: int = 0
    cleanups: int = 0
    service_factory: Any = lambda _config, _ctx: object()
    cleanup: Any = None

    def setup(self, ctx: Context, config: Any) -> Disposable:
        self.calls += 1
        services = [ctx.register(key, self.service_factory(config, ctx)) for key in self.provides]

        async def teardown() -> None:
            self.cleanups += 1
            if self.cleanup is not None:
                await self.cleanup()
            for service in reversed(services):
                await service.dispose()

        return Disposable(teardown)


@pytest.mark.asyncio
async def test_plugin_dormant_activation_removal_and_reactivation() -> None:
    agent = _agent()
    plugin = _Plugin(provides=("out",), requires=("dep",))
    mounted = await agent.use(plugin)
    assert plugin.calls == 0

    dependency = agent.context.register("dep", 0)
    await _settle()
    assert plugin.calls == 1
    assert agent.context.get("out") is not None

    await dependency.dispose()
    assert agent.context.get("out") is None
    assert plugin.cleanups == 1

    dependency = agent.context.register("dep", False)
    await _settle()
    assert plugin.calls == 2
    assert agent.context.get("out") is not None

    await mounted.dispose()
    assert agent.context.get("out") is None


@pytest.mark.asyncio
async def test_dependency_removal_rebinds_to_inherited_fallback() -> None:
    parent = Context()
    inherited = object()
    parent.register("dep", inherited)
    agent = _agent(context=parent)
    shadow = agent.context.register("dep", object())
    seen: list[object] = []

    class BindingPlugin:
        name = "binding"
        version = "1"
        provides = ("out",)
        requires = ("dep",)

        def setup(self, ctx: Context, config: Any) -> Disposable:
            seen.append(ctx.get("dep"))
            return ctx.register("out", object())

    mounted = await agent.use(BindingPlugin())
    await shadow.dispose()
    await _settle()
    assert seen[0] is not inherited
    assert seen[-1] is inherited
    assert len(seen) == 2
    await mounted.dispose()


@pytest.mark.asyncio
async def test_plugin_dependencies_chain_and_unmount_cascade() -> None:
    agent = _agent()
    first = _Plugin(name="first", provides=("first",))
    second = _Plugin(name="second", requires=("first",), provides=("second",))
    first_mount = await agent.use(first)
    second_mount = await agent.use(second)
    assert first.calls == second.calls == 1
    assert agent.context.get("second") is not None

    await first_mount.dispose()
    assert agent.context.get("first") is None
    assert agent.context.get("second") is None
    assert second.cleanups == 1

    await agent.use(first)
    await _settle()
    assert first.calls == 2
    assert second.calls == 2
    assert agent.context.get("second") is not None
    await second_mount.dispose()


@pytest.mark.asyncio
async def test_disposing_plugin_service_registration_withdraws_export_and_dependents() -> None:
    agent = _agent()

    class ManualPlugin:
        name = "manual"
        version = "1"
        provides = ("manual",)
        requires: tuple[str, ...] = ()
        registration: Disposable | None = None

        def setup(self, ctx: Context, config: Any) -> Disposable:
            self.registration = ctx.register("manual", object())
            return Disposable()

    manual = ManualPlugin()
    dependent = _Plugin(name="dependent", requires=("manual",), provides=("dependent",))
    await agent.use(manual)
    await agent.use(dependent)
    assert agent.context.get("dependent") is not None

    assert manual.registration is not None
    await manual.registration.dispose()
    await _settle()
    assert agent.context.get("manual") is None
    assert agent.context.get("dependent") is None
    assert dependent.cleanups == 1


@pytest.mark.asyncio
async def test_plugin_audit_rejects_undeclared_service_and_rolls_back() -> None:
    agent = _agent()
    cleaned = False

    class BadPlugin:
        name = "bad"
        version = "1"
        provides: tuple[str, ...] = ()
        requires: tuple[str, ...] = ()

        def setup(self, ctx: Context, config: Any) -> Disposable:
            ctx.register("leak", object())

            async def teardown() -> None:
                nonlocal cleaned
                cleaned = True

            return Disposable(teardown)

    with pytest.raises(ConfigError, match="provides"):
        await agent.use(BadPlugin())
    assert agent.context.get("leak") is None
    assert cleaned


@pytest.mark.asyncio
async def test_setup_failure_rolls_back_owned_async_cleanup_before_use_raises() -> None:
    agent = _agent()
    cleaned = False

    class FailingPlugin:
        name = "failing"
        version = "1"
        provides = ("leak",)
        requires: tuple[str, ...] = ()
        scope: Context | None = None

        def setup(self, ctx: Context, config: Any) -> Disposable:
            nonlocal cleaned
            self.scope = ctx
            ctx.register("leak", object())

            async def cleanup() -> None:
                nonlocal cleaned
                await asyncio.sleep(0)
                cleaned = True

            ctx.effect(lambda: cleanup)
            raise RuntimeError("setup failed")

    plugin = FailingPlugin()
    with pytest.raises(RuntimeError, match="setup failed"):
        await agent.use(plugin)
    assert cleaned
    assert agent.context.get("leak") is None
    assert plugin.scope is not None and plugin.scope._scope.disposed


@pytest.mark.asyncio
async def test_closed_plugin_source_is_not_returned_from_stale_export() -> None:
    agent = _agent()
    source: Context | None = None

    class SourcePlugin:
        name = "source"
        version = "1"
        provides = ("out",)
        requires: tuple[str, ...] = ()

        def setup(self, ctx: Context, config: Any) -> Disposable:
            nonlocal source
            source = ctx
            return ctx.register("out", object())

    await agent.use(SourcePlugin())
    assert source is not None
    await source.dispose()
    assert agent.context.get("out") is None


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_plugin_rejects_setup_escape_to_captured_parent() -> None:
    parent = Context()
    agent = _agent(context=parent)

    class EscapePlugin:
        name = "escape"
        version = "1"
        provides: tuple[str, ...] = ()
        requires: tuple[str, ...] = ()

        def setup(self, ctx: Context, config: Any) -> Disposable:
            parent.register("outside", object())
            return Disposable()

    with pytest.raises(ConfigError, match="activation scope"):
        await agent.use(EscapePlugin())
    assert parent.get("outside") is None


@pytest.mark.asyncio
async def test_plugin_rejects_setup_escape_to_unrelated_agent_context() -> None:
    parent = Context()
    agent = _agent(context=parent)
    unrelated = _agent(context=parent)

    class EscapePlugin:
        name = "unrelated-escape"
        version = "1"
        provides: tuple[str, ...] = ()
        requires: tuple[str, ...] = ()

        def setup(self, ctx: Context, config: Any) -> Disposable:
            unrelated.context.register("outside", object())
            return Disposable()

    with pytest.raises(ConfigError, match="activation scope"):
        await agent.use(EscapePlugin())
    assert unrelated.context.get("outside") is None


@pytest.mark.asyncio
async def test_plugin_cleanup_retry_withdraws_services_before_cleanup() -> None:
    agent = _agent()
    attempts = 0

    async def fail_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("cleanup failed")

    plugin = _Plugin(provides=("out",), cleanup=fail_once)
    mounted = await agent.use(plugin)
    assert agent.context.get("out") is not None

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await mounted.dispose()
    assert agent.context.get("out") is None
    await mounted.dispose()
    assert attempts == 2


@pytest.mark.asyncio
async def test_closed_mount_never_reactivates_after_blocked_cleanup() -> None:
    agent = _agent()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_cleanup() -> None:
        started.set()
        await release.wait()

    plugin = _Plugin(provides=("out",), requires=("dep",), cleanup=blocked_cleanup)
    mounted = await agent.use(plugin)
    dependency = agent.context.register("dep", object())
    await _settle()
    removal = asyncio.create_task(dependency.dispose())
    await started.wait()
    unmount = asyncio.create_task(mounted.dispose())
    replacement = agent.context.register("dep", object())
    await _settle()
    assert plugin.calls == 1

    release.set()
    await removal
    await unmount
    assert agent.context.get("out") is None
    await replacement.dispose()


@pytest.mark.asyncio
async def test_plugins_are_isolated_between_agents_sharing_parent() -> None:
    parent = Context()
    first = _agent(context=parent)
    second = _agent(context=parent)
    plugin = _Plugin(provides=("out",))
    first_mount = await first.use(plugin)
    second_mount = await second.use(plugin)
    assert first.context.get("out") is not None
    assert second.context.get("out") is not None
    assert parent.get("out") is None

    await first_mount.dispose()
    assert first.context.get("out") is None
    assert second.context.get("out") is not None
    await second_mount.dispose()


@pytest.mark.asyncio
async def test_duplicate_plugin_provision_rolls_back_second_mount() -> None:
    agent = _agent()
    first = _Plugin(name="first", provides=("same",))
    second = _Plugin(name="second", provides=("same",))
    first_mount = await agent.use(first)
    with pytest.raises(ConfigError, match="already provided"):
        await agent.use(second)
    assert agent.context.get("same") is not None
    assert second.cleanups == 1
    await first_mount.dispose()

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
    from linch.ext.evals import ScriptedProvider

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
async def test_stale_plugin_export_does_not_hide_inherited_dependency_change() -> None:
    parent = Context()
    inherited = object()
    parent_registration = parent.register("dep", inherited)
    agent = _agent(context=parent)
    seen: list[object] = []

    class ManualPlugin:
        name = "manual-dep"
        version = "1"
        provides = ("dep",)
        requires: tuple[str, ...] = ()
        registration: Disposable | None = None

        def setup(self, ctx: Context, config: Any) -> Disposable:
            self.registration = ctx.register("dep", object())
            return Disposable()

    class Dependent:
        name = "fallback-dependent"
        version = "1"
        provides = ("dependent",)
        requires = ("dep",)

        def setup(self, ctx: Context, config: Any) -> Disposable:
            seen.append(ctx.get("dep"))
            return ctx.register("dependent", object())

    manual = ManualPlugin()
    manual_mount = await agent.use(manual)
    dependent_mount = await agent.use(Dependent())
    assert manual.registration is not None
    await manual.registration.dispose()
    await _settle()
    assert seen[-1] is inherited

    await parent_registration.dispose()
    await _settle()
    assert agent.context.get("dependent") is None

    await dependent_mount.dispose()
    await manual_mount.dispose()
    await agent.close()


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
async def test_plugin_setup_cannot_mutate_captured_parent_through_any_context_surface() -> None:
    parent = Context()
    agent = _agent(context=parent)

    operations = {
        "register": lambda: parent.register("outside", object()),
        "on": lambda: parent.on("outside", lambda: None),
        "effect": lambda: parent.effect(lambda: None),
        "scope": lambda: parent.scope("outside"),
        "inbox": lambda: parent.inbox(lambda: None),
    }

    for name, operation in operations.items():
        plugin_name = f"escape-{name}"

        class EscapePlugin:
            name = plugin_name
            version = "1"
            provides: tuple[str, ...] = ()
            requires: tuple[str, ...] = ()

            def __init__(self, action: Any) -> None:
                self.action = action

            def setup(self, ctx: Context, config: Any) -> Disposable:
                self.action()
                return Disposable()

        with pytest.raises(ConfigError, match="activation scope"):
            await agent.use(EscapePlugin(operation))
    assert parent.get("outside") is None


@pytest.mark.asyncio
async def test_plugin_rejects_a_key_declared_as_both_requirement_and_provision() -> None:
    agent = _agent()
    dependency = agent.context.register("loop", object())
    setup_calls = 0

    class CircularPlugin:
        name = "circular"
        version = "1"
        provides = ("loop",)
        requires = ("loop",)

        def setup(self, ctx: Context, config: Any) -> Disposable:
            nonlocal setup_calls
            setup_calls += 1
            return ctx.register("loop", object())

    with pytest.raises(ConfigError, match="cycle"):
        await agent.use(CircularPlugin())
    assert setup_calls == 0
    await dependency.dispose()
    assert agent.context.get("loop") is None


@pytest.mark.asyncio
async def test_plugin_unmount_withdraws_exports_before_async_cleanup() -> None:
    agent = _agent()
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowPlugin:
        name = "slow"
        version = "1"
        provides = ("slow",)
        requires: tuple[str, ...] = ()

        def setup(self, ctx: Context, config: Any) -> Disposable:
            registration = ctx.register("slow", object())

            async def cleanup() -> None:
                assert agent.context.get("slow") is None
                started.set()
                await release.wait()

            ctx.effect(lambda: cleanup)
            return registration

    mounted = await agent.use(SlowPlugin())
    assert agent.context.get("slow") is not None
    disposing = asyncio.create_task(mounted.dispose())
    await started.wait()
    assert agent.context.get("slow") is None
    release.set()
    await disposing


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


@pytest.mark.asyncio
async def test_indirect_dependency_cycle_is_rejected_before_setup_repeats() -> None:
    parent = Context()
    parent.register("a", object())
    parent.register("b", object())
    agent = _agent(context=parent)
    first = _Plugin(name="first", provides=("a",), requires=("b",))
    second = _Plugin(name="second", provides=("b",), requires=("a",))

    await agent.use(first)
    with pytest.raises(ConfigError, match="cycle"):
        await agent.use(second)
    await _settle()

    assert first.calls == 1
    assert second.calls == 0
    assert agent.context.get("a") is not None
    assert agent.context.get("b") is not None


@pytest.mark.asyncio
async def test_closed_dependent_retries_before_provider_cleanup_finishes() -> None:
    agent = _agent()
    attempts = 0
    order: list[str] = []

    class Provider:
        name = "provider"
        version = "1"
        provides = ("a",)
        requires: tuple[str, ...] = ()

        def setup(self, ctx: Context, config: Any) -> Disposable:
            ctx.register("a", object())
            return Disposable(lambda: order.append("provider"))

    class Dependent:
        name = "dependent-retry"
        version = "1"
        provides: tuple[str, ...] = ()
        requires = ("a",)

        def setup(self, ctx: Context, config: Any) -> Disposable:
            async def cleanup() -> None:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise RuntimeError("dependent cleanup failed")
                order.append("dependent")

            return Disposable(cleanup)

    provider_mount = await agent.use(Provider())
    dependent_mount = await agent.use(Dependent())

    with pytest.raises(RuntimeError, match="dependent cleanup failed"):
        await provider_mount.dispose()
    assert attempts == 1
    assert order == []

    with pytest.raises(RuntimeError, match="dependent cleanup failed"):
        await dependent_mount.dispose()
    assert attempts == 2
    assert order == []

    await provider_mount.dispose()
    assert attempts == 3
    assert order == ["dependent", "provider"]
    await dependent_mount.dispose()


@pytest.mark.asyncio
async def test_cancelled_setup_failure_keeps_cleanup_running_until_release() -> None:
    agent = _agent()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = False

    class Failing:
        name = "cancelled"
        version = "1"
        provides: tuple[str, ...] = ()
        requires: tuple[str, ...] = ()
        scope: Context | None = None

        def setup(self, ctx: Context, config: Any) -> Disposable:
            nonlocal finished
            self.scope = ctx

            async def cleanup() -> None:
                nonlocal finished
                started.set()
                await release.wait()
                finished = True

            ctx.effect(lambda: cleanup)
            raise RuntimeError("setup failed")

    plugin = Failing()
    use_task = asyncio.create_task(agent.use(plugin))
    await started.wait()
    use_task.cancel()
    await asyncio.sleep(0)
    assert not use_task.done()
    assert not finished

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await use_task
    assert finished
    assert plugin.scope is not None and plugin.scope._scope.disposed


@pytest.mark.asyncio
async def test_sibling_service_events_do_not_retry_other_agent_cleanup() -> None:
    parent = Context()
    parent_dependency = parent.register("dep", object())
    first = _agent(context=parent)
    second = _agent(context=parent)
    same_key = first.context.register("dep", object())
    unrelated = first.context.register("unrelated", object())
    attempts = 0

    class Dependent:
        name = "sibling-dependent"
        version = "1"
        provides: tuple[str, ...] = ()
        requires = ("dep",)

        def setup(self, ctx: Context, config: Any) -> Disposable:
            async def cleanup() -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("dependent cleanup failed")

            return Disposable(cleanup)

    mounted = await second.use(Dependent())
    await same_key.dispose()
    assert attempts == 0

    with pytest.raises(RuntimeError, match="dependent cleanup failed"):
        await parent_dependency.dispose()
    assert attempts == 1

    await unrelated.dispose()
    assert attempts == 1
    await mounted.dispose()
    assert attempts == 2
    await first.close()
    await second.close()


@pytest.mark.asyncio
async def test_relevant_withdrawal_joins_a_busy_dependent_cleanup() -> None:
    agent = _agent()
    trigger = agent.context.register("trigger", object())
    started = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    class Provider:
        name = "busy-provider"
        version = "1"
        provides = ("dep",)
        requires: tuple[str, ...] = ()

        def setup(self, ctx: Context, config: Any) -> Disposable:
            ctx.register("dep", object())
            return Disposable(lambda: order.append("provider"))

    class Dependent:
        name = "busy-dependent"
        version = "1"
        provides: tuple[str, ...] = ()
        requires = ("dep", "trigger")

        def setup(self, ctx: Context, config: Any) -> Disposable:
            async def cleanup() -> None:
                started.set()
                await release.wait()
                order.append("dependent")

            return Disposable(cleanup)

    provider = await agent.use(Provider())
    dependent = await agent.use(Dependent())
    trigger_removal = asyncio.create_task(trigger.dispose())
    await started.wait()
    provider_removal = asyncio.create_task(provider.dispose())
    await asyncio.sleep(0)
    assert order == []

    release.set()
    await trigger_removal
    await provider_removal
    assert order == ["dependent", "provider"]
    await dependent.dispose()

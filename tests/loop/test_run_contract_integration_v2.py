"""Runtime integration and security coverage for durable run contracts."""

from __future__ import annotations

from typing import Any

import pytest


class _Provider:
    id = "contract-provider"

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req: Any):
        from linch.types import Usage

        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


def _agent(
    *,
    run_store: Any,
    session_store: Any,
    permissions: Any = None,
    hooks: Any = None,
    tools: Any = None,
    execution_backend: Any = None,
):
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.tools.registry import empty_tools

    return Agent(
        model="test-model",
        provider=_Provider(),
        tools=tools or empty_tools(),
        permissions=permissions or {"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        hooks=hooks,
        features=FeatureFlags(),
        result_offload=None,
        execution_backend=execution_backend,
    )


async def _interrupt_after_first_event(session: Any) -> str:
    iterator = session.run("go").__aiter__()
    event = await iterator.__anext__()
    assert event.type == "system"
    await iterator.aclose()
    return event.run_id


async def test_permission_rule_change_denies_resume() -> None:
    from linch.errors import ConfigError
    from linch.permissions import PathRule
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    first = _agent(
        run_store=run_store,
        session_store=session_store,
        permissions={
            "mode": "skip-dangerous",
            "rules": [PathRule(paths=["safe/**"], decision="allow")],
        },
    )
    run_id = await _interrupt_after_first_event(await first.session(id="s1"))

    changed = _agent(
        run_store=run_store,
        session_store=session_store,
        permissions={
            "mode": "skip-dangerous",
            "rules": [PathRule(paths=["safe/**"], decision="deny")],
        },
    )
    resumed = await changed.session(id="s1")
    with pytest.raises(ConfigError, match=r"permissions.*decision"):
        _ = [event async for event in resumed.resume(run_id)]


async def test_callback_policy_version_change_denies_resume() -> None:
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class Callback:
        resume_policy_id = "approval-policy"

        def __init__(self, version: str) -> None:
            self.resume_policy_version = version

        async def __call__(self, request: Any) -> dict[str, str]:
            return {"behavior": "allow"}

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    first = _agent(
        run_store=run_store,
        session_store=session_store,
        permissions={"mode": "default", "canUseTool": Callback("1")},
    )
    run_id = await _interrupt_after_first_event(await first.session(id="s1"))
    changed = _agent(
        run_store=run_store,
        session_store=session_store,
        permissions={"mode": "default", "canUseTool": Callback("2")},
    )
    with pytest.raises(ConfigError, match="policy_version"):
        _ = [event async for event in (await changed.session(id="s1")).resume(run_id)]


@pytest.mark.parametrize("kind", ["callback", "hook"])
async def test_opaque_custom_policy_rejected_for_durable_run(kind: str) -> None:
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class Callback:
        async def __call__(self, request: Any) -> dict[str, str]:
            return {"behavior": "allow"}

    class Hook:
        def on_pre_tool_use(self, ctx: Any) -> None:
            return None

    agent = _agent(
        run_store=InMemoryRunStore(),
        session_store=InMemorySessionStore(),
        permissions=(
            {"mode": "default", "canUseTool": Callback()}
            if kind == "callback"
            else {"mode": "skip-dangerous"}
        ),
        hooks=[Hook()] if kind == "hook" else None,
    )
    session = await agent.session()
    with pytest.raises(ConfigError, match="resume_policy_id"):
        _ = [event async for event in session.run("go")]


async def test_docker_backend_configuration_change_denies_resume() -> None:
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools import workspace_tools
    from linch.tools.execution import DockerBackend

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    first = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=workspace_tools(),
        execution_backend=DockerBackend(docker_path="/opt/docker-v1", network="none"),
    )
    run_id = await _interrupt_after_first_event(await first.session(id="s1"))

    changed = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=workspace_tools(),
        execution_backend=DockerBackend(docker_path="/opt/docker-v2", network="none"),
    )
    with pytest.raises(ConfigError, match="docker_path"):
        _ = [event async for event in (await changed.session(id="s1")).resume(run_id)]


async def test_docker_path_lookup_does_not_change_durable_contract(monkeypatch: Any) -> None:
    import shutil

    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools import workspace_tools
    from linch.tools.execution import DockerBackend

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    monkeypatch.setattr(shutil, "which", lambda _: "/host-a/docker")
    first = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=workspace_tools(),
        execution_backend=DockerBackend(docker_path=None, network="none"),
    )
    run_id = await _interrupt_after_first_event(await first.session(id="s1"))

    monkeypatch.setattr(shutil, "which", lambda _: "/host-b/docker")
    resumed = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=workspace_tools(),
        execution_backend=DockerBackend(docker_path=None, network="none"),
    )
    events = [event async for event in (await resumed.session(id="s1")).resume(run_id)]

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"


async def test_docker_backend_contract_hashes_environment_values() -> None:
    import json

    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools import workspace_tools
    from linch.tools.execution import DockerBackend

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    fingerprint_key = b"test-only-fingerprint-key"
    first = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=workspace_tools(),
        execution_backend=DockerBackend(
            docker_path="/opt/docker",
            image="python@sha256:fixture",
            env={"API_KEY": "do-not-store-raw"},
            resume_fingerprint_key=fingerprint_key,
        ),
    )
    run_id = await _interrupt_after_first_event(await first.session(id="s1"))
    stored = await run_store.load_run(run_id)
    assert stored is not None
    serialized = json.dumps(stored.meta, sort_keys=True)
    assert "do-not-store-raw" not in serialized
    assert "hmac-sha256:" in serialized

    changed = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=workspace_tools(),
        execution_backend=DockerBackend(
            docker_path="/opt/docker",
            image="python@sha256:fixture",
            env={"API_KEY": "changed"},
            resume_fingerprint_key=fingerprint_key,
        ),
    )
    with pytest.raises(ConfigError, match="API_KEY"):
        _ = [event async for event in (await changed.session(id="s1")).resume(run_id)]


async def test_durable_docker_environment_requires_fingerprint_key() -> None:
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools import workspace_tools
    from linch.tools.execution import DockerBackend

    agent = _agent(
        run_store=InMemoryRunStore(),
        session_store=InMemorySessionStore(),
        tools=workspace_tools(),
        execution_backend=DockerBackend(env={"STAGE": "dev"}),
    )
    with pytest.raises(ConfigError, match="resume_fingerprint_key"):
        _ = [event async for event in (await agent.session()).run("go")]


async def test_slots_only_custom_bash_backend_requires_stable_identity() -> None:
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools import workspace_tools

    class OpaqueBackend:
        __slots__ = ()

        async def run(self, command: str, **kwargs: Any) -> Any:
            raise AssertionError("backend must not execute in this test")

    agent = _agent(
        run_store=InMemoryRunStore(),
        session_store=InMemorySessionStore(),
        tools=workspace_tools(),
        execution_backend=OpaqueBackend(),
    )
    with pytest.raises(ConfigError, match="custom Bash execution backend.*resume_policy_id"):
        _ = [event async for event in (await agent.session()).run("go")]


async def test_custom_bash_backend_requires_explicit_stable_config() -> None:
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools import workspace_tools

    class IdOnlyBackend:
        __slots__ = ()
        resume_policy_id = "custom-shell"

        async def run(self, command: str, **kwargs: Any) -> Any:
            raise AssertionError("backend must not execute in this test")

    agent = _agent(
        run_store=InMemoryRunStore(),
        session_store=InMemorySessionStore(),
        tools=workspace_tools(),
        execution_backend=IdOnlyBackend(),
    )
    with pytest.raises(ConfigError, match="non-None backend.resume_policy_config"):
        _ = [event async for event in (await agent.session()).run("go")]


async def test_custom_bash_backend_config_change_denies_resume() -> None:
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools import workspace_tools

    class StableBackend:
        __slots__ = ("endpoint",)
        resume_policy_id = "custom-shell"
        resume_policy_version = "1"

        def __init__(self, endpoint: str) -> None:
            self.endpoint = endpoint

        @property
        def resume_policy_config(self) -> dict[str, str]:
            return {"endpoint": self.endpoint}

        async def run(self, command: str, **kwargs: Any) -> Any:
            raise AssertionError("backend must not execute in this test")

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    first = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=workspace_tools(),
        execution_backend=StableBackend("sandbox-a"),
    )
    run_id = await _interrupt_after_first_event(await first.session(id="s1"))
    changed = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=workspace_tools(),
        execution_backend=StableBackend("sandbox-b"),
    )
    with pytest.raises(ConfigError, match="endpoint"):
        _ = [event async for event in (await changed.session(id="s1")).resume(run_id)]


async def test_policy_adapter_requires_host_stable_identity() -> None:
    from linch.errors import ConfigError
    from linch.hooks import FinalAnswerVerifierHook
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class Verifier:
        name = "opaque"

        def verify(self, ctx: Any) -> Any:
            return None

    agent = _agent(
        run_store=InMemoryRunStore(),
        session_store=InMemorySessionStore(),
        hooks=[FinalAnswerVerifierHook(Verifier())],
    )
    with pytest.raises(ConfigError, match="FinalAnswerVerifierHook.*resume_policy_id"):
        _ = [event async for event in (await agent.session()).run("go")]


async def test_checkpoint_key_does_not_replace_hook_policy_identity() -> None:
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class CheckpointOnlyHook:
        checkpoint_key = "custom.state"

        def checkpoint_state(self, session: Any, run_id: str) -> dict[str, Any]:
            return {}

        def restore_checkpoint_state(
            self, state: dict[str, Any], session: Any, run_id: str
        ) -> None:
            return None

    agent = _agent(
        run_store=InMemoryRunStore(),
        session_store=InMemorySessionStore(),
        hooks=[CheckpointOnlyHook()],
    )
    with pytest.raises(ConfigError, match="checkpoint_key alone"):
        _ = [event async for event in (await agent.session()).run("go")]

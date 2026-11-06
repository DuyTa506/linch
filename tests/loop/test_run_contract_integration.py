"""Runtime integration and security coverage for durable run contracts."""

from __future__ import annotations

from typing import Any

import pytest

from linch.errors import ConfigError
from linch.run_store import InMemoryRunStore
from linch.sessions import InMemorySessionStore
from linch.tools import workspace_tools
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


class _ContractShell:
    async def run(self, command: str, **kwargs: Any) -> Any:
        raise AssertionError("backend must not execute in this test")


class _ContractFs:
    resume_policy_id = "test.contract-fs"
    resume_policy_version = "1"

    @property
    def resume_policy_config(self) -> dict[str, str]:
        return {"root": "contract-fixture"}

    async def read(self, path: str, **kwargs: Any) -> str:
        raise AssertionError("backend must not execute in this test")

    async def write(self, path: str, content: str) -> None:
        raise AssertionError("backend must not execute in this test")

    async def ls(self, prefix: str = "") -> list[str]:
        raise AssertionError("backend must not execute in this test")

    async def edit(self, path: str, old: str, new: str, **kwargs: Any) -> int:
        raise AssertionError("backend must not execute in this test")

    async def exists(self, path: str) -> bool:
        raise AssertionError("backend must not execute in this test")

    async def delete(self, path: str) -> None:
        raise AssertionError("backend must not execute in this test")


class _OpaqueWorld:
    def __init__(self) -> None:
        self.shell = _ContractShell()
        self.fs = _ContractFs()


class _IdOnlyWorld(_OpaqueWorld):
    resume_policy_id = "custom-world"


class _StableTransport:
    resume_policy_id = "custom-transport"
    resume_policy_version = "1"

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    @property
    def resume_policy_config(self) -> dict[str, str]:
        return {"endpoint": self.endpoint}


class _StableWorld:
    resume_policy_id = "custom-world"
    resume_policy_version = "1"

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.shell = _StableTransport(endpoint)
        self.fs = _StableTransport(endpoint)

    @property
    def resume_policy_config(self) -> dict[str, str]:
        return {"endpoint": self.endpoint}


def _agent(
    *,
    run_store: Any,
    session_store: Any,
    permissions: Any = None,
    hooks: Any = None,
    tools: Any = None,
    execution_backend: Any = None,
):
    return make_agent(
        ScriptedProvider([TextTurn("done")]),
        tools=tools,
        permissions=permissions or {"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        hooks=hooks,
        execution_backend=execution_backend,
        result_offload=None,
    )


async def _interrupt_after_first_event(session: Any) -> str:
    iterator = session.run("go").__aiter__()
    event = await iterator.__anext__()
    assert event.type == "system"
    await iterator.aclose()
    return event.run_id


async def test_permission_rule_change_denies_resume() -> None:
    from linch.permissions import PathRule

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
        permissions={"mode": "default", "can_use_tool": Callback("1")},
    )
    run_id = await _interrupt_after_first_event(await first.session(id="s1"))
    changed = _agent(
        run_store=run_store,
        session_store=session_store,
        permissions={"mode": "default", "can_use_tool": Callback("2")},
    )
    with pytest.raises(ConfigError, match="policy_version"):
        _ = [event async for event in (await changed.session(id="s1")).resume(run_id)]


@pytest.mark.parametrize("kind", ["callback", "hook"])
async def test_opaque_custom_policy_rejected_for_durable_run(kind: str) -> None:
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
            {"mode": "default", "can_use_tool": Callback()}
            if kind == "callback"
            else {"mode": "skip-dangerous"}
        ),
        hooks=[Hook()] if kind == "hook" else None,
    )
    session = await agent.session()
    with pytest.raises(ConfigError, match="resume_policy_id"):
        _ = [event async for event in session.run("go")]


@pytest.mark.parametrize("kind", ["callback", "hook"])
async def test_legacy_policy_attributes_do_not_identify_durable_policy(kind: str) -> None:
    class Callback:
        policy_id = "legacy-policy"
        policy_version = "1"
        resume_policy_config: dict[str, Any] = {}

        async def __call__(self, request: Any) -> dict[str, str]:
            return {"behavior": "allow"}

    class Hook:
        policy_id = "legacy-policy"
        policy_version = "1"
        resume_policy_config: dict[str, Any] = {}

        def on_pre_tool_use(self, ctx: Any) -> None:
            return None

    agent = _agent(
        run_store=InMemoryRunStore(),
        session_store=InMemorySessionStore(),
        permissions=(
            {"mode": "default", "can_use_tool": Callback()}
            if kind == "callback"
            else {"mode": "skip-dangerous"}
        ),
        hooks=[Hook()] if kind == "hook" else None,
    )
    session = await agent.session()
    with pytest.raises(ConfigError, match="resume_policy_id"):
        _ = [event async for event in session.run("go")]


def _docker_world(**kwargs: Any) -> Any:
    from linch.execution import RemoteExecutionBackend
    from linch.tools.execution import DockerBackend

    shell = DockerBackend(**kwargs)
    return RemoteExecutionBackend(
        shell=shell,
        fs=_ContractFs(),
        confinement=shell.confinement,
        resume_policy_id="test.docker-world",
        resume_policy_version="1",
        resume_policy_config={"transport": "docker"},
    )


async def test_docker_backend_configuration_change_denies_resume() -> None:
    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    first = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=workspace_tools(),
        execution_backend=_docker_world(docker_path="/opt/docker-v1", network="none"),
    )
    run_id = await _interrupt_after_first_event(await first.session(id="s1"))

    changed = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=workspace_tools(),
        execution_backend=_docker_world(docker_path="/opt/docker-v2", network="none"),
    )
    with pytest.raises(ConfigError, match="docker_path"):
        _ = [event async for event in (await changed.session(id="s1")).resume(run_id)]


async def test_durable_docker_environment_requires_fingerprint_key() -> None:
    """The env-without-key misconfiguration is the error the caller must see.

    Degrading it to "this shell has no resume_policy" names a symptom and
    hides the fix, and would let the env drop out of the durable fingerprint.
    """
    agent = _agent(
        run_store=InMemoryRunStore(),
        session_store=InMemorySessionStore(),
        tools=workspace_tools(),
        execution_backend=_docker_world(env={"STAGE": "dev"}),
    )
    with pytest.raises(ConfigError, match="requires resume_fingerprint_key"):
        _ = [event async for event in (await agent.session()).run("go")]


@pytest.mark.parametrize(
    ("world", "message"),
    [
        (_OpaqueWorld(), "custom execution world.*resume_policy_id"),
        (_IdOnlyWorld(), "custom execution world.*resume_policy_config"),
    ],
)
async def test_custom_bash_backend_requires_stable_identity(world: Any, message: str) -> None:
    agent = _agent(
        run_store=InMemoryRunStore(),
        session_store=InMemorySessionStore(),
        tools=workspace_tools(),
        execution_backend=world,
    )
    with pytest.raises(ConfigError, match=message):
        _ = [event async for event in (await agent.session()).run("go")]


async def test_custom_bash_backend_config_change_denies_resume() -> None:
    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    first = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=workspace_tools(),
        execution_backend=_StableWorld("sandbox-a"),
    )
    run_id = await _interrupt_after_first_event(await first.session(id="s1"))
    changed = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=workspace_tools(),
        execution_backend=_StableWorld("sandbox-b"),
    )
    with pytest.raises(ConfigError, match="endpoint"):
        _ = [event async for event in (await changed.session(id="s1")).resume(run_id)]


@pytest.mark.parametrize("kind", ["verifier", "checkpoint"])
async def test_policy_adapter_requires_host_stable_identity(kind: str) -> None:
    from linch.hooks import FinalAnswerVerifierHook

    class Verifier:
        name = "opaque"

        def verify(self, ctx: Any) -> Any:
            return None

    class CheckpointOnlyHook:
        checkpoint_key = "custom.state"

        def checkpoint_state(self, session: Any, run_id: str) -> dict[str, Any]:
            return {}

        def restore_checkpoint_state(
            self, state: dict[str, Any], session: Any, run_id: str
        ) -> None:
            return None

    hook = FinalAnswerVerifierHook(Verifier()) if kind == "verifier" else CheckpointOnlyHook()
    message = (
        "FinalAnswerVerifierHook.*resume_policy_id"
        if kind == "verifier"
        else "checkpoint_key alone"
    )
    agent = _agent(run_store=InMemoryRunStore(), session_store=InMemorySessionStore(), hooks=[hook])
    with pytest.raises(ConfigError, match=message):
        _ = [event async for event in (await agent.session()).run("go")]


@pytest.mark.parametrize(
    ("change", "path"),
    [
        ("name", r"tools\[0\]\.name"),
        ("input_schema", r"tools\[0\]\.input_schema"),
        ("scope", r"tools\[0\]\.scope"),
        ("parallel", r"tools\[0\]\.parallel"),
        ("output_schema", r"tools\[0\]\.output_schema"),
        ("input_description", r"tools\[0\]\.input_schema.*description"),
        ("output_description", r"tools\[0\]\.output_schema.*description"),
    ],
)
async def test_live_tool_contract_changes_reject_resume(change: str, path: str) -> None:
    from tests.support.tools import StubTool, tool_registry

    first_tool = StubTool("probe", scope="exec", parallel=False)
    first_tool.input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string", "description": "old input"}},
    }
    first_tool.output_schema = {"type": "string", "description": "old output"}
    first_registry = tool_registry(first_tool)
    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    first = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=first_registry,
    )
    first_session = await first.session(id="tool-contract")
    first_session.tools_override = first_registry
    run_id = await _interrupt_after_first_event(first_session)

    changed_tool = StubTool("probe", scope="exec", parallel=False)
    changed_tool.input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string", "description": "old input"}},
    }
    changed_tool.output_schema = {"type": "string", "description": "old output"}
    if change == "name":
        changed_tool.name = "renamed"
    elif change == "input_schema":
        changed_tool.input_schema = {"type": "object", "properties": {"value": {"type": "integer"}}}
    elif change == "scope":
        changed_tool.scope = "read"
    elif change == "parallel":
        changed_tool.parallel = True
    elif change == "output_schema":
        changed_tool.output_schema = {"type": "integer"}
    elif change == "input_description":
        changed_tool.input_schema["properties"]["value"]["description"] = "new input"
    else:
        changed_tool.output_schema["description"] = "new output"
    changed_registry = tool_registry(changed_tool)
    changed = _agent(
        run_store=run_store,
        session_store=session_store,
        tools=changed_registry,
    )
    resumed_session = await changed.session(id="tool-contract")
    resumed_session.tools_override = changed_registry
    with pytest.raises(ConfigError, match=path):
        _ = [event async for event in resumed_session.resume(run_id)]

from __future__ import annotations

from typing import Any

import pytest

from linch import Principal, RunOptions
from linch.run_store import InMemoryRunStore, run_meta_with_contract
from linch.sessions import InMemorySessionStore
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.stores import memory_run_store, memory_session_store
from tests.support.tools import tool_registry


class _PrincipalTool:
    name = "PrincipalTool"
    description = "Captures principal context."
    input_schema = {"type": "object"}
    scope = "read"
    parallel = False

    def __init__(self) -> None:
        self.seen: list[Any] = []

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    async def execute(self, input: dict[str, Any], ctx: Any) -> Any:
        self.seen.append(ctx.principal)
        from linch.tools import ToolResult

        return ToolResult(content="ok")


def _agent(
    provider: Any,
    tool: Any,
    run_store: Any = None,
    session_store: Any = None,
    *,
    permissions: Any = None,
    enable_background_tools: bool = False,
) -> Any:
    options: dict[str, Any] = {
        "tools": tool_registry(tool),
        "permissions": permissions or {"mode": "skip-dangerous"},
        "result_offload": None,
        "enable_background_tools": enable_background_tools,
    }
    if run_store is not None:
        options["run_store"] = run_store
    else:
        options["run_store"] = memory_run_store()
    if session_store is not None:
        options["session_store"] = session_store
    else:
        options["session_store"] = memory_session_store()
    return make_agent(
        provider,
        **options,
    )


async def _interrupt(session: Any, opts: Any) -> str:
    iterator = session.run("go", opts).__aiter__()
    event = await iterator.__anext__()
    await iterator.aclose()
    return event.run_id


@pytest.mark.asyncio
async def test_resume_rejects_tenant_user_or_role_change_before_provider_dispatch() -> None:
    from linch.errors import ConfigError

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    first = _agent(ScriptedProvider([TextTurn("done")]), _PrincipalTool(), run_store, session_store)
    original = Principal("tenant-a", "alice", {"reader"})
    run_id = await _interrupt(await first.session(id="s"), RunOptions(principal=original))
    await first.close()

    provider = ScriptedProvider([TextTurn("must-not-run")])
    changed = _agent(provider, _PrincipalTool(), run_store, session_store)
    for replacement in (
        Principal("tenant-b", "alice", {"reader"}),
        Principal("tenant-a", "bob", {"reader"}),
        Principal("tenant-a", "alice", {"admin"}),
    ):
        with pytest.raises(ConfigError, match="run_options.*principal"):
            _ = [
                event
                async for event in (await changed.session(id="s")).resume(
                    run_id, RunOptions(principal=replacement)
                )
            ]
    assert provider.requests == []


@pytest.mark.asyncio
async def test_pending_run_adoption_hydrates_omitted_principal() -> None:
    from linch.loop.contracts import _build_run_contract

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    agent = _agent(ScriptedProvider([TextTurn("done")]), _PrincipalTool(), run_store, session_store)
    session = await agent.session(id="s")
    principal = Principal("tenant-a", "alice", {"reader"})
    contract = _build_run_contract(session, "go", RunOptions(principal=principal))
    await run_store.create_run("s", id="pending", meta=run_meta_with_contract(contract))
    run_store._runs["pending"].status = "pending"

    events = [event async for event in session.run("go", RunOptions(run_id="pending"))]
    assert events[-1].subtype == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("original_guard", "changed_guard"),
    [("role", "operator"), ("unless_role", "operator")],
)
async def test_changed_role_policy_rejects_resume(original_guard: str, changed_guard: str) -> None:
    from linch.errors import ConfigError
    from linch.permissions import ToolRule

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    principal = Principal("tenant-a", "alice", {"reader"})
    first = _agent(
        ScriptedProvider([TextTurn("done")]),
        _PrincipalTool(),
        run_store,
        session_store,
        permissions={
            "mode": "skip-dangerous",
            "rules": [ToolRule("PrincipalTool", "deny", **{original_guard: "reader"})],
        },
    )
    run_id = await _interrupt(await first.session(id="s"), RunOptions(principal=principal))
    await first.close()

    provider = ScriptedProvider([TextTurn("must-not-run")])
    changed = _agent(
        provider,
        _PrincipalTool(),
        run_store,
        session_store,
        permissions={
            "mode": "skip-dangerous",
            "rules": [ToolRule("PrincipalTool", "deny", **{original_guard: changed_guard})],
        },
    )
    with pytest.raises(ConfigError, match="policies.permissions"):
        _ = [
            event
            async for event in (await changed.session(id="s")).resume(
                run_id, RunOptions(principal=principal)
            )
        ]
    assert provider.requests == []


@pytest.mark.asyncio
async def test_malformed_stored_principal_is_rejected_after_valid_contract_recompute() -> None:
    from linch.errors import ConfigError
    from linch.run_store import _run_contract_fingerprint, run_contract_from_meta

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    principal = Principal("tenant-a", "alice", {"reader"})
    first = _agent(
        ScriptedProvider([TextTurn("done")]),
        _PrincipalTool(),
        run_store,
        session_store,
    )
    run_id = await _interrupt(await first.session(id="s"), RunOptions(principal=principal))
    await first.close()

    raw = run_store._runs[run_id].meta["run_contract"]
    assert isinstance(raw, dict)
    payload = raw["payload"]
    assert isinstance(payload, dict)
    options = payload["run_options"]
    assert isinstance(options, dict)
    options["principal"] = {"tenant": "tenant-a", "user": "alice", "roles": "reader"}
    raw["fingerprint"] = _run_contract_fingerprint(raw["schema_version"], payload)
    run_contract_from_meta(run_store._runs[run_id].meta)

    provider = ScriptedProvider([TextTurn("must-not-run")])
    resumed = _agent(provider, _PrincipalTool(), run_store, session_store)
    with pytest.raises(ConfigError, match="invalid principal shape"):
        _ = [event async for event in (await resumed.session(id="s")).resume(run_id, RunOptions())]
    assert provider.requests == []


@pytest.mark.asyncio
async def test_same_run_resume_preserves_pending_background_principal() -> None:
    import asyncio

    from linch.types import Usage

    entered = asyncio.Event()
    resume_started = asyncio.Event()
    resume_release = asyncio.Event()
    gate = asyncio.Event()

    class _ResumeProvider:
        id = "same-run-resume"

        def __init__(self) -> None:
            self.calls = 0

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, request: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                yield {"type": "message_start", "model": request.model}
                yield {"type": "tool_use_start", "id": "bg", "name": "PrincipalTool"}
                yield {
                    "type": "tool_use_input_delta",
                    "id": "bg",
                    "json_delta": '{"run_in_background":true}',
                }
                yield {"type": "tool_use_end", "id": "bg"}
                yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
                return
            resume_started.set()
            await resume_release.wait()
            yield {"type": "message_start", "model": request.model}
            yield {"type": "text_delta", "text": "resumed"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    class _BlockingPrincipalTool(_PrincipalTool):
        parallel = True

        async def execute(self, input: dict[str, Any], ctx: Any) -> Any:
            entered.set()
            await gate.wait()
            self.seen.append(ctx.principal)
            from linch.tools import ToolResult

            return ToolResult(content="background-complete")

    principal = Principal("tenant-a", "alice", {"reader"})
    tool = _BlockingPrincipalTool()
    agent = _agent(
        _ResumeProvider(),
        tool,
        InMemoryRunStore(),
        InMemorySessionStore(),
        enable_background_tools=True,
    )
    session = await agent.session(id="s")
    iterator = session.run("go", RunOptions(principal=principal)).__aiter__()
    event = await iterator.__anext__()
    run_id = event.run_id
    while True:
        event = await iterator.__anext__()
        if event.type == "tool_call_end":
            break
    old_tasks = list(session.background_tasks)
    assert old_tasks and not entered.is_set()
    await iterator.aclose()

    resume_iterator = session.resume(run_id, RunOptions()).__aiter__()
    resume_task = asyncio.create_task(_collect_events(resume_iterator))
    await resume_started.wait()
    gate.set()
    await asyncio.gather(*old_tasks)
    assert len(tool.seen) == 1
    assert tool.seen[0] is principal
    resume_release.set()
    events = await resume_task
    assert events[-1].subtype == "success"
    await agent.close()


async def _collect_events(iterator: Any) -> list[Any]:
    return [event async for event in iterator]

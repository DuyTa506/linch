from __future__ import annotations

from typing import Any

import pytest

from tests.support.agents import make_agent as support_make_agent
from tests.support.providers import ScriptedProvider, TextTurn


class Provider(ScriptedProvider):
    """Replay RAG answer text while retaining provider request inspection."""

    def __init__(self, answers: list[str] | None = None) -> None:
        super().__init__([TextTurn(answer) for answer in (answers or ["answer [doc:a]"])])


class Connector:
    resume_policy_id = "test-docs"
    resume_policy_version = 1
    resume_policy_config = {"scope": "tenant-a"}

    def __init__(self, ids: tuple[str, ...] = ("a",)) -> None:
        self.ids = ids
        self.calls: list[tuple[str, int, Any]] = []

    async def retrieve(self, query: str, *, k: int, principal: Any = None):
        from linch import RetrievedChunk

        self.calls.append((query, k, principal))
        return [
            RetrievedChunk(i, f"source-{i}", f"PRIVATE BODY {i}", 0.8, {"secret": "private"})
            for i in self.ids
        ]


def make_agent(provider=None, **kwargs):
    """Build a non-durable RAG test agent."""

    kwargs.setdefault("run_store", None)
    return support_make_agent(provider or Provider(), **kwargs)


async def drain(session, prompt="original question"):
    return [event async for event in session.run(prompt)]


async def test_request_only_retrieval_and_persisted_typed_audit():
    from linch import ContextBuildEvent, InMemoryRunStore, register_rag_connector
    from linch.events import event_to_dict

    provider, connector, principal, store = Provider(), Connector(), object(), InMemoryRunStore()
    agent = make_agent(provider, run_store=store)
    register_rag_connector(agent, connector, k=3, principal=principal)
    session = await agent.session()
    events = await drain(session)
    assert connector.calls == [("original question", 3, principal)]
    assert any(
        "[doc:a]" in block.text and "source-a" in block.text and "PRIVATE BODY a" in block.text
        for block in provider.requests[0].system
    )
    assert "PRIVATE BODY" not in str(session.full_history)
    loaded = await session.store.load_messages(session.id)
    assert "PRIVATE BODY" not in str(loaded)
    audit = next(event for event in events if isinstance(event, ContextBuildEvent))
    assert audit.metadata == {"rag": {"ids": ["a"], "sources": ["source-a"]}}
    run_id = next(event.run_id for event in events if event.type == "system")
    persisted = [row.event for row in await store.load_events(run_id)]
    assert any(
        isinstance(event, ContextBuildEvent) and event.metadata == audit.metadata
        for event in persisted
    )
    assert "PRIVATE BODY" not in str([event_to_dict(event) for event in persisted])
    await agent.close()


@pytest.mark.parametrize("k", [0, -1, True, 1.5])
async def test_rejects_invalid_k(k):
    from linch import register_rag_connector

    agent = make_agent()
    with pytest.raises(ValueError, match="positive"):
        register_rag_connector(agent, Connector(), k=k)
    await agent.close()


@pytest.mark.parametrize(
    "answer,success",
    [
        ("answer [doc:a]", True),
        ("answer", False),
        ("[doc:unknown]", False),
        ("[doc:a] [doc:unknown]", False),
    ],
)
async def test_grounding_accepts_only_current_known_citations(answer, success):
    from linch import register_grounding_check, register_rag_connector

    provider = Provider([answer])
    agent = make_agent(provider, reliability=None)
    register_rag_connector(agent, Connector())
    register_grounding_check(agent)
    events = await drain(await agent.session())
    assert (events[-1].subtype == "success") is success
    assert len(provider.requests) == 1
    if not success:
        assert any(event.type == "verification" and event.action == "stop" for event in events)
    await agent.close()


@pytest.mark.parametrize("cap", [0, 1, 2])
@pytest.mark.parametrize("corrected", [False, True])
async def test_grounding_corrections_are_bounded_and_preserve_query(cap, corrected):
    from linch import ReliabilityPolicy, register_grounding_check, register_rag_connector

    answers = ["unsupported"] * cap + (["answer [doc:a]"] if corrected else ["unsupported"])
    provider, connector = Provider(answers), Connector()
    agent = make_agent(provider, reliability=ReliabilityPolicy(max_correction_turns=cap))
    register_rag_connector(agent, connector)
    register_grounding_check(agent)
    session = await agent.session()
    events = await drain(session)
    assert len(provider.requests) == cap + 1
    assert (events[-1].subtype == "success") is corrected
    assert [query for query, _, _ in connector.calls] == ["original question"] * (cap + 1)
    assert sum(event.type == "verification" and event.action == "retry" for event in events) == cap
    assert "PRIVATE BODY" not in str(session.full_history)
    await agent.close()


@pytest.mark.parametrize("connector,required", [(False, True), (True, False)])
async def test_grounding_without_connector_or_when_optional_is_noop(connector, required):
    from linch import register_grounding_check, register_rag_connector

    agent = make_agent(Provider(["unsupported"]))
    register_grounding_check(agent, required=required)
    if connector:
        register_rag_connector(agent, Connector())
    events = await drain(await agent.session())
    assert events[-1].subtype == "success"
    assert not any(event.type == "verification" for event in events)
    await agent.close()


async def test_empty_retrieval_is_audited_and_cannot_ground():
    from linch import register_grounding_check, register_rag_connector

    agent = make_agent(reliability=None)
    register_rag_connector(agent, Connector(()))
    register_grounding_check(agent)
    events = await drain(await agent.session())
    assert events[-1].subtype == "error"
    assert next(event for event in events if event.type == "context_build").metadata == {
        "rag": {"ids": [], "sources": []}
    }
    await agent.close()


async def test_retrieval_failure_is_visible():
    from linch import register_grounding_check, register_rag_connector

    class Broken(Connector):
        async def retrieve(self, *args, **kwargs):
            raise RuntimeError("retrieval unavailable")

    provider = Provider()
    agent = make_agent(provider)
    register_rag_connector(agent, Broken())
    register_grounding_check(agent)
    events = await drain(await agent.session())
    assert events[-1].subtype == "error"
    assert any(event.type == "error" and "retrieval unavailable" in str(event) for event in events)
    assert provider.requests == []
    await agent.close()


async def test_budget_trimming_preserves_retrieved_citation_membership():
    from linch import (
        ContextBudget,
        ContextBuildResult,
        ContextInjectionHook,
        register_grounding_check,
        register_rag_connector,
    )

    class Trim:
        async def build(self, turn):
            return ContextBuildResult(budget=ContextBudget(max_tokens=0))

    provider = Provider()
    agent = make_agent(provider, reliability=None)
    register_rag_connector(agent, Connector())
    agent.hooks.append(ContextInjectionHook(Trim()))
    register_grounding_check(agent)
    events = await drain(await agent.session(), "user quotes [doc:a]")
    assert events[-1].subtype == "success"
    assert all("[doc:a]" not in block.text for block in provider.requests[0].system)
    await agent.close()


async def test_disposal_is_identity_safe_and_restores_baseline():
    from linch import ConfigError, register_grounding_check, register_rag_connector
    from linch.events import event_to_dict

    agent = make_agent(Provider(["unsupported"]))
    baseline_hooks = list(agent.hooks)
    retrieval = register_rag_connector(agent, Connector())
    grounding = register_grounding_check(agent)
    with pytest.raises(ConfigError, match="already registered"):
        register_rag_connector(agent, Connector())
    await retrieval.dispose()
    replacement = register_rag_connector(agent, Connector(("b",)))
    await retrieval.dispose()
    await replacement.dispose()
    await grounding.dispose()
    assert agent.hooks == baseline_hooks
    assert agent.context.get("rag_connector") is None
    assert not agent._turn_stopping_dispatcher.active
    one = await drain(await agent.session())
    two = await drain(await make_agent(Provider(["unsupported"])).session())

    def normalized(events):
        return [
            {
                key: value
                for key, value in event_to_dict(event).items()
                if key not in {"session_id", "run_id", "trace_id", "duration_ms", "timestamp", "id"}
            }
            for event in events
        ]

    assert normalized(one) == normalized(two)
    await agent.close()


async def test_shared_context_siblings_and_concurrent_sessions_do_not_share_chunks():
    import asyncio

    from linch import Context, RetrievedChunk, register_grounding_check, register_rag_connector

    class Scoped(Connector):
        async def retrieve(self, query, *, k, principal=None):
            await asyncio.sleep(0)
            return [RetrievedChunk(query, query, "private", 1.0, {})]

    parent = Context()
    a = make_agent(Provider(["[doc:a]"]), context=parent.scope("a"), reliability=None)
    b = make_agent(Provider(["[doc:a]"]), context=parent.scope("b"), reliability=None)
    register_rag_connector(a, Scoped())
    register_grounding_check(a)
    first, second, sibling = await a.session(), await a.session(), await b.session()
    results = await asyncio.gather(drain(first, "a"), drain(second, "b"), drain(sibling, "b"))
    assert [events[-1].subtype for events in results] == ["success", "error", "success"]
    hook = a.context.get("rag_connector")
    assert hook.runs == {}
    await parent.dispose()
    assert hook not in a.hooks


async def test_checkpoint_preserves_query_ids_and_correction_bound():
    from linch import (
        Message,
        ReliabilityPolicy,
        TextBlock,
        Usage,
        register_grounding_check,
        register_rag_connector,
    )
    from linch.hooks.contexts import UserPromptSubmitContext
    from linch.loop.turn_stopping import TurnStopping
    from linch.types import AssistantAssembly

    agent = make_agent(reliability=ReliabilityPolicy(max_correction_turns=1))
    register_rag_connector(agent, Connector())
    register_grounding_check(agent)
    session = await agent.session()
    session.active_run_id = "run"
    hook = agent.context.get("rag_connector")
    hook.on_user_prompt_submit(UserPromptSubmitContext(session, "run", 0, prompt="question"))
    await hook.build_context(session, 0)
    assembly = AssistantAssembly(
        Message(role="assistant", content=[TextBlock(text="unsupported")]), "end_turn", Usage()
    )
    assert (
        await agent._turn_stopping_dispatcher.dispatch(
            TurnStopping(session, "run", 0, "final_text", assembly)
        )
    ).action == "continue"
    state = hook.checkpoint_state(session, "run")
    assert state == {"query": "question", "ids": ["a"], "count": 1}
    await hook.on_agent_stop(UserPromptSubmitContext(session, "run", 0))
    hook.restore_checkpoint_state(state, session, "run")
    assert (
        await agent._turn_stopping_dispatcher.dispatch(
            TurnStopping(session, "run", 1, "final_text", assembly)
        )
    ).action == "fail"
    valid = AssistantAssembly(
        Message(role="assistant", content=[TextBlock(text="[doc:a]")]), "end_turn", Usage()
    )
    assert (
        await agent._turn_stopping_dispatcher.dispatch(
            TurnStopping(session, "run", 1, "final_text", valid)
        )
    ).action == "accept"
    for invalid in [
        {},
        {"query": "q", "ids": [1], "count": 0},
        {"query": "q", "ids": [], "count": -1},
        {"query": "q", "ids": [], "count": 2},
        {"query": "q", "ids": [], "count": True},
    ]:
        with pytest.raises(ValueError, match="checkpoint"):
            hook.restore_checkpoint_state(invalid, session, "run")
    await agent.close()


@pytest.mark.parametrize("id", ["", "a]b", "a\nb", "a\rb"])
async def test_uncitable_ids_fail_visibly(id):
    from linch import register_rag_connector

    provider = Provider()
    agent = make_agent(provider)
    register_rag_connector(agent, Connector((id,)))
    events = await drain(await agent.session())
    assert events[-1].subtype == "error"
    assert provider.requests == []
    await agent.close()


async def test_inherited_retrieval_hook_cannot_expose_or_clear_owning_agent_state():
    from linch import register_rag_connector
    from linch.hooks.contexts import AgentStopContext, UserPromptSubmitContext

    owner = make_agent()
    register_rag_connector(owner, Connector())
    hook = owner.context.get("rag_connector")
    sibling = make_agent(hooks=[hook])
    one, two = await owner.session(id="same"), await sibling.session(id="same")
    hook.on_user_prompt_submit(UserPromptSubmitContext(one, "same-run", 0, prompt="secret"))
    assert hook.checkpoint_state(two, "same-run") == {}
    await hook.on_agent_stop(AgentStopContext(two, "same-run", 0))
    assert hook.checkpoint_state(one, "same-run")["query"] == "secret"
    await owner.close()
    await sibling.close()


@pytest.mark.parametrize("answer,success", [("[doc:a]", False), ("[doc:b]", True)])
async def test_context_length_recovery_replaces_retrieved_ids_and_audit(answer, success):
    from dataclasses import dataclass

    from linch import ContextLengthError, register_grounding_check, register_rag_connector

    class ChangingConnector(Connector):
        async def retrieve(self, query, *, k, principal=None):
            self.ids = ("a",) if not self.calls else ("b",)
            return await super().retrieve(query, k=k, principal=principal)

    class RecoveringProvider(Provider):
        async def stream(self, request):
            if not self.requests:
                self.requests.append(request)
                raise ContextLengthError("retry with compacted context")
            async for event in super().stream(request):
                yield event

    @dataclass
    class NoopCompaction:
        id: str = "noop"

        async def compact(self, ctx, provider):
            return list(ctx.messages)

    provider, connector = RecoveringProvider([answer]), ChangingConnector()
    agent = make_agent(provider, reliability=None)
    agent.compaction = NoopCompaction()
    register_rag_connector(agent, connector)
    register_grounding_check(agent)
    events = await drain(await agent.session())
    assert (events[-1].subtype == "success") is success
    assert [event.metadata["rag"] for event in events if event.type == "context_build"] == [
        {"ids": ["a"], "sources": ["source-a"]},
        {"ids": ["b"], "sources": ["source-b"]},
    ]
    assert len(provider.requests) == 2
    assert any("[doc:b]" in block.text for block in provider.requests[-1].system)
    assert all(query == "original question" for query, _, _ in connector.calls)
    await agent.close()


@pytest.mark.parametrize("profiled", [False, True])
async def test_empty_profile_preserves_host_retrieval_and_grounding(profiled):
    from linch import Profile, RunOptions, register_grounding_check, register_rag_connector

    provider, connector = Provider(["unsupported"]), Connector(("d1",))
    agent = make_agent(provider, reliability=None)
    register_rag_connector(agent, connector)
    register_grounding_check(agent)
    session = await agent.session()
    opts = RunOptions(profile=Profile("empty", "1")) if profiled else None
    events = [event async for event in session.run("question", opts)]
    assert connector.calls == [("question", 5, None)]
    assert any("[doc:d1]" in block.text for block in provider.requests[0].system)
    assert events[-1].subtype != "success"
    assert session.agent is agent
    await agent.close()


async def test_profile_connector_shadows_host_without_duplicate_retrieval():
    from linch import Profile, RunOptions, register_rag_connector
    from linch.kernel import Disposable

    host, local = Connector(("host",)), Connector(("local",))

    class LocalRag:
        name, version = "local-rag", "1"
        provides = ("rag_connector",)
        requires = ("run_config",)

        def setup(self, ctx, config=None):
            from linch.rag import _register_rag_connector

            ctx.effect(lambda: _register_rag_connector(ctx.run_config, ctx, local))
            return Disposable()

    provider = Provider(["answer [doc:local]"])
    agent = make_agent(provider)
    register_rag_connector(agent, host)
    session = await agent.session()
    events = [
        event
        async for event in session.run(
            "question", RunOptions(profile=Profile("local", "1", plugins={"rag": (LocalRag(), {})}))
        )
    ]
    assert host.calls == []
    assert len(local.calls) == 1
    assert events[-1].subtype == "success"
    assert any("[doc:local]" in block.text for block in provider.requests[0].system)
    assert agent.context.get("rag_connector").connector is host
    await agent.close()

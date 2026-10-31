"""Request-scoped sourced retrieval and optional bounded citation checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..context import ContextBuildResult
from ..events import VerificationEvent
from ..hooks import ContextInjectionHook
from ..kernel import Disposable
from ..loop.turn_stopping import TurnStopping, TurnStoppingDecision
from ..types import Message, SystemBlock, TextBlock

if TYPE_CHECKING:
    from ..agent import Agent


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    id: str
    source: str
    text: str
    score: float
    metadata: dict[str, Any]


class RagConnector(Protocol):
    async def retrieve(
        self, query: str, *, k: int, principal: Any = None
    ) -> list[RetrievedChunk]: ...


class _Retrieval(ContextInjectionHook):
    name = checkpoint_key = "linch.rag"
    resume_policy_id = "linch.rag"
    resume_policy_version = 1

    def __init__(self, agent: Agent, connector: RagConnector, k: int, principal: Any) -> None:
        self.agent, self.connector, self.k, self.principal = agent, connector, k, principal
        self.runs: dict[tuple[str, str], dict[str, Any]] = {}
        self.resume_policy_config = {
            "k": k,
            "connector": {
                key: getattr(connector, "resume_policy_" + key, None)
                for key in ("id", "version", "config")
            },
        }
        super().__init__(self)

    def state(self, session: Any, run_id: str) -> dict[str, Any]:
        return self.runs.setdefault((session.id, run_id), {"query": "", "ids": [], "count": 0})

    def on_user_prompt_submit(self, ctx: Any) -> None:
        if ctx.session.agent is self.agent:
            self.state(ctx.session, ctx.run_id)["query"] = ctx.prompt

    async def build(self, turn: Any) -> ContextBuildResult:
        if turn.session.agent is not self.agent:
            return ContextBuildResult()
        key = (turn.session.id, turn.session.active_run_id)
        state = self.state(turn.session, key[1])
        chunks = await self.connector.retrieve(state["query"], k=self.k, principal=self.principal)
        if any(not isinstance(c.id, str) or not re.fullmatch(r"[^\]\r\n]+", c.id) for c in chunks):
            raise ValueError("RAG chunk ids must be nonempty and exclude ], CR and LF")
        blocks = [
            (c.id, SystemBlock(text=f"[doc:{c.id}]\nSource: {c.source}\n{c.text}")) for c in chunks
        ]
        state["ids"] = [c.id for c in chunks]
        return ContextBuildResult(
            system_blocks=[block for _, block in blocks],
            metadata={"rag": {"ids": state["ids"], "sources": [c.source for c in chunks]}},
        )

    async def on_agent_stop(self, ctx: Any) -> None:
        if ctx.session.agent is not self.agent:
            return
        self.runs.pop((ctx.session.id, ctx.run_id), None)

    def checkpoint_state(self, session: Any, run_id: str) -> dict[str, Any]:
        state = self.runs.get((session.id, run_id)) if session.agent is self.agent else None
        return {**state, "ids": list(state["ids"])} if state else {}

    def restore_checkpoint_state(self, state: dict[str, Any], session: Any, run_id: str) -> None:
        if session.agent is not self.agent:
            return
        cap = getattr(self.agent.reliability, "max_correction_turns", 0)
        if (
            set(state) != {"query", "ids", "count"}
            or not isinstance(state["query"], str)
            or not isinstance(state["ids"], list)
            or any(not isinstance(id, str) for id in state["ids"])
            or type(state["count"]) is not int
            or not 0 <= state["count"] <= cap
        ):
            raise ValueError("invalid RAG checkpoint state")
        self.runs[(session.id, run_id)] = {**state, "ids": list(state["ids"])}


def _install(agent: Agent, hook: Any, service: Disposable) -> Disposable:
    agent.hooks.append(hook)

    async def remove() -> None:
        agent.hooks[:] = [item for item in agent.hooks if item is not hook]
        await service.dispose()
        if isinstance(hook, _Retrieval):
            hook.runs.clear()

    return agent.context.effect(lambda: Disposable(remove))


def register_rag_connector(
    agent: Agent, connector: RagConnector, *, k: int = 5, principal: Any = None
) -> Disposable:
    """Install one request-scoped connector; principal is passed through unchanged."""
    if type(k) is not int or k <= 0:
        raise ValueError("k must be a positive int")
    hook = _Retrieval(agent, connector, k, principal)
    return _install(agent, hook, agent.context.register("rag_connector", hook))


class _Grounding:
    name = resume_policy_id = "linch.rag.grounding"
    resume_policy_version = 1

    def __init__(self, agent: Agent, required: bool) -> None:
        self.agent = agent
        self.resume_policy_config = {"required": required}

    def __call__(self, event: TurnStopping) -> TurnStoppingDecision | None:
        hook = self.agent.context.get("rag_connector")
        if (
            not self.resume_policy_config["required"]
            or not isinstance(hook, _Retrieval)
            or hook.agent is not self.agent
            or event.session.agent is not self.agent
            or event.reason == "invalid_response"
        ):
            return None
        state = hook.state(event.session, event.run_id)
        value = (
            event.final_block.input
            if event.final_block
            else (
                [
                    block.text
                    for block in event.assembly.message.content
                    if isinstance(block, TextBlock)
                ]
                if event.assembly
                else []
            )
        )
        cited = set(re.findall(r"\[doc:([^\]\r\n]+)\]", "\n".join(_strings(value))))
        ids = set(state["ids"])
        if cited and cited <= ids:
            return None
        exhausted = state["count"] >= getattr(self.agent.reliability, "max_correction_turns", 0)
        if not exhausted:
            state["count"] += 1
        feedback = (
            "[Linch] Grounding requires a citation [doc:<id>] "
            "from the latest retrieved sources only."
        )
        return TurnStoppingDecision(
            "fail" if exhausted else "continue",
            feedback=()
            if exhausted
            else (Message(role="user", content=[TextBlock(text=feedback)]),),
            error=ValueError("RAG grounding correction budget exhausted") if exhausted else None,
            event=VerificationEvent(
                "rag_grounding", "stop" if exhausted else "retry", feedback, state["count"]
            ),
        )


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    values = (
        value.values()
        if isinstance(value, dict)
        else value
        if isinstance(value, (list, tuple))
        else ()
    )
    return [text for item in values for text in _strings(item)]


def register_grounding_check(agent: Agent, *, required: bool = True) -> Disposable:
    """Require citations to the latest retrieved ids, with a separate per-run correction bound."""
    hook = _Grounding(agent, required)
    service = agent.context.register("rag_grounding", hook)
    listener = agent.on_turn_stopping(hook)
    installed = _install(agent, hook, service)

    async def remove() -> None:
        await listener.dispose()
        await installed.dispose()

    return agent.context.effect(lambda: Disposable(remove))

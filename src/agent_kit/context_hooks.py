"""Per-turn context injection hooks.

This module provides :class:`ContextInjector`, a protocol for mutating the
provider's message list and system blocks before each LLM call.

Unlike observability :class:`~agent_kit.hooks.RunHooks` (which must not
mutate state), context injectors are explicitly designed to modify
``provider_view`` and add ephemeral ``extra_system`` blocks.  The canonical
use-case is RAG-per-turn: retrieve relevant documents and append them to the
conversation before each provider call so they're always in context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .session import Session
    from .types import Message, SystemBlock


@dataclass(slots=True)
class TurnContext:
    """Context passed to a :class:`ContextInjector` before each provider call.

    Attributes:
        session:
            The current :class:`~agent_kit.session.Session`.
        provider_view:
            The live message list that will be sent to the provider.  This is
            the same object as ``session.provider_view``; appending or pruning
            here directly affects what the model sees.
        turn_index:
            Zero-based index of the current agent turn within this run.
        deps:
            The resolved application-state dependency object (from
            ``Agent(deps=...)`` or ``RunOptions(deps=...)``).
        extra_system:
            Append :class:`~agent_kit.types.SystemBlock` objects here to
            inject ephemeral system text for this turn only.  These are merged
            into the ``ProviderRequest.system`` list and do **not** persist
            across turns.
    """

    session: Session
    provider_view: list[Message]
    turn_index: int
    deps: Any
    extra_system: list[SystemBlock] = field(default_factory=list)


class ContextInjector(Protocol):
    """Protocol for per-turn context injection.

    Implement this interface to append retrieved documents, database schemas,
    or any other dynamic context to ``provider_view`` before each provider
    call.

    Example — RAG injector::

        TAG = "[[RAG]]"

        class RagInjector:
            def __init__(self, vector_store):
                self.store = vector_store

            async def before_turn(self, ctx: TurnContext) -> None:
                # Remove last turn's injected context
                prune_tagged(ctx.provider_view, TAG)
                # Retrieve fresh context based on the last user message
                docs = await self.store.search(last_user_text(ctx))
                if docs:
                    from agent_kit.types import Message, TextBlock
                    ctx.provider_view.append(Message(
                        role="user",
                        content=[TextBlock(text=f"{TAG}\\nRetrieved context:\\n{docs}")],
                    ))
    """

    async def before_turn(self, ctx: TurnContext) -> None:
        """Called once per turn, before the ProviderRequest is assembled.

        Mutate ``ctx.provider_view`` and/or ``ctx.extra_system`` as needed.
        The method is ``async`` so I/O-bound operations (vector searches, DB
        queries) work naturally.
        """
        ...


def prune_tagged(messages: list[Message], tag: str) -> None:
    """Remove messages whose first text block starts with *tag*, in-place.

    Use this at the start of :meth:`ContextInjector.before_turn` to remove
    the previous turn's injected messages before adding fresh ones.  This
    prevents the provider view from growing unboundedly over many turns.

    Args:
        messages: The ``provider_view`` list (or any ``list[Message]``).
        tag: String prefix to match.  A message is pruned when its first
            :class:`~agent_kit.types.TextBlock` starts with this tag.

    Example::

        TAG = "[[retrieved]]"
        prune_tagged(ctx.provider_view, TAG)
        ctx.provider_view.append(Message(...))  # fresh injection
    """
    from .types import TextBlock

    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.content and isinstance(msg.content[0], TextBlock):
            if msg.content[0].text.startswith(tag):
                messages.pop(i)
                continue
        i += 1

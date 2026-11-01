"""Common agent builders for unit tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .providers import ScriptedProvider, TextTurn
from .stores import memory_run_store, memory_session_store

if TYPE_CHECKING:
    from linch import Agent

_DEFAULT = object()


def make_agent(
    provider: Any,
    *,
    tools: Any = None,
    session_store: Any = None,
    run_store: Any = _DEFAULT,
    model: str = "test-model",
    **overrides: Any,
) -> Agent:
    """Build an inert test agent with in-memory stores by default.

    Args:
        provider: Provider used for model calls.
        tools: Optional tool registry; omitted means an empty registry.
        session_store: Session store, defaulting to a fresh in-memory store.
        run_store: Durable store, defaulting to a fresh in-memory store; ``None``
            explicitly disables durable runs.
        model: Model identifier exposed to the provider.
        overrides: Additional ``Agent`` keyword arguments, which take precedence.

    Returns:
        A configured Linch agent.
    """

    from linch import Agent
    from linch.config import FeatureFlags
    from linch.tools.registry import empty_tools

    options = dict(overrides)
    options.setdefault("permissions", {"mode": "skip-dangerous"})
    options.setdefault("cwd", ".")
    options.setdefault("features", FeatureFlags())
    options.setdefault("provider", provider)
    options.setdefault("model", model)
    options.setdefault("tools", tools if tools is not None else empty_tools())
    options.setdefault(
        "session_store", session_store if session_store is not None else memory_session_store()
    )
    if run_store is not _DEFAULT:
        options.setdefault("run_store", run_store)
    else:
        options.setdefault("run_store", memory_run_store())
    return Agent(**options)


def make_worker_agent(
    job: Any, provider: Any = None, context: Any = None, **overrides: Any
) -> Agent:
    """Build the agent used by a worker job's factory.

    Args:
        job: Worker job exposing session and run stores.
        provider: Optional provider; defaults to one scripted text turn.
        context: Optional host context passed to the agent.
        overrides: Additional agent options.

    Returns:
        An agent wired to the job's stores.
    """

    provider = provider or ScriptedProvider([TextTurn("done")])
    from linch.tools.registry import empty_tools

    options = {
        "session_store": job.session_store,
        "run_store": job.run_store,
        "tools": empty_tools(),
    }
    if context is not None:
        options["context"] = context
    options.update(overrides)
    options.setdefault("model", "m")
    return make_agent(provider, **options)

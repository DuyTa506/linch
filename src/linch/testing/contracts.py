from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from ..coordination.mailbox import Mailbox, MailboxMessage
from ..filesystem import FileBackend
from ..tools.base import ResourceAccess, ToolContext, ToolResult
from ..tools.isolation import IsolationBackend

T = TypeVar("T")


async def assert_file_backend_contract(
    factory: Callable[[], FileBackend | Awaitable[FileBackend]],
) -> None:
    """Assert the core behavioral contract for a ``FileBackend`` implementation.

    Args:
        factory: Zero-argument callable returning a fresh backend, either
            directly or through an awaitable.

    Raises:
        AssertionError: If the backend violates the file contract.
    """

    backend = await _call_factory(factory)
    try:
        assert await backend.ls() == [], "fresh backend must list no files"
        assert not await backend.exists("/notes/a.txt"), "missing path must not exist"
        await _assert_raises(FileNotFoundError, backend.read("/missing.txt"))

        await backend.write("notes//a.txt/", "line1\nline2\nline3")
        assert await backend.exists("/notes/a.txt")
        assert await backend.exists("notes/a.txt")
        assert await backend.read("/notes/a.txt") == "line1\nline2\nline3"
        assert await backend.read("/notes/a.txt", offset=2, limit=1) == "line2"
        assert await backend.read("/notes/a.txt", offset=2) == "line2\nline3"

        await backend.write("/notes/b.txt", "bee")
        await backend.write("/other.txt", "other")
        assert await backend.ls() == ["/notes/a.txt", "/notes/b.txt", "/other.txt"]
        assert await backend.ls("/notes") == ["/notes/a.txt", "/notes/b.txt"]
        assert await backend.ls("/notes/a.txt") == ["/notes/a.txt"]

        count = await backend.edit("/notes/a.txt", "line2", "LINE2")
        assert count == 1
        assert await backend.read("/notes/a.txt") == "line1\nLINE2\nline3"
        await _assert_raises(ValueError, backend.edit("/notes/a.txt", "missing", "x"))

        await backend.write("/repeat.txt", "x x x")
        await _assert_raises(ValueError, backend.edit("/repeat.txt", "x", "y"))
        count = await backend.edit("/repeat.txt", "x", "y", replace_all=True)
        assert count == 3
        assert await backend.read("/repeat.txt") == "y y y"

        await backend.delete("/notes/a.txt")
        assert not await backend.exists("/notes/a.txt")
        await _assert_raises(FileNotFoundError, backend.read("/notes/a.txt"))
        await backend.delete("/notes/a.txt")
    finally:
        await _maybe_close(backend)


async def assert_isolation_backend_contract(
    factory: Callable[[], IsolationBackend | Awaitable[IsolationBackend]],
) -> None:
    """Assert the core behavioral contract for an ``IsolationBackend`` implementation.

    Args:
        factory: Zero-argument callable returning a fresh backend, either
            directly or through an awaitable.

    Raises:
        AssertionError: If the backend violates the isolation contract.
    """

    backend = await _call_factory(factory)
    first: str | None = None
    second: str | None = None
    try:
        first = await backend.acquire()
        second = await backend.acquire()
        first_path = Path(first)
        second_path = Path(second)

        assert first_path.is_dir(), "acquire must return a usable directory"
        assert second_path.is_dir(), "acquire must return a usable directory"
        assert first_path != second_path, "distinct acquire calls must not reuse the same cwd"

        (first_path / "shared.txt").write_text("from-first", encoding="utf-8")
        (second_path / "shared.txt").write_text("from-second", encoding="utf-8")
        assert (first_path / "shared.txt").read_text(encoding="utf-8") == "from-first"
        assert (second_path / "shared.txt").read_text(encoding="utf-8") == "from-second"

        await backend.release(first, keep=True)
        assert first_path.exists(), "release(..., keep=True) must preserve the cwd"
        first = None

        await backend.release(second)
        assert not second_path.exists(), "release(..., keep=False) must remove the cwd"
        second = None
    finally:
        if first is not None:
            await _release_ignore(backend, first)
        if second is not None:
            await _release_ignore(backend, second)


async def assert_mailbox_contract(
    factory: Callable[[], Mailbox | Awaitable[Mailbox]],
) -> None:
    """Assert the core behavioral contract for a ``Mailbox`` implementation.

    ``factory`` must return a fresh, empty mailbox. The check is designed for
    pytest suites in adapter packages: ``await assert_mailbox_contract(lambda:
    MyMailbox(...))``.

    Delivery checks compare complete ``MailboxMessage`` values, including their
    type and request/reply correlation fields, rather than only message IDs.

    Args:
        factory: Zero-argument callable returning a fresh mailbox, either
            directly or through an awaitable.

    Raises:
        AssertionError: If the mailbox violates delivery or lifecycle
            guarantees.
    """

    box = await _call_factory(factory)
    try:
        assert await box.drain("missing") == [], "drain of an unknown recipient must be empty"

        isolated = MailboxMessage(
            sender="alice",
            recipient="bob",
            content="for-bob",
            type="request",
            request_id="request-1",
            in_reply_to="prior-1",
            id="isolated",
        )
        await box.send(isolated)
        assert await box.drain("carol") == [], "drain must be isolated per recipient"
        drained = await box.drain("bob")
        assert drained == [isolated], "sent message fields were not preserved"
        assert await box.drain("bob") == [], "drain must be destructive"

        fifo = [
            MailboxMessage(sender="alice", recipient="bob", content=str(index))
            for index in range(5)
        ]
        for message in fifo:
            await box.send(message)
        drained = await box.drain("bob")
        assert drained == fifo, "drain must preserve sequential message fields and order"

        concurrent = [
            MailboxMessage(sender="alice", recipient="bob", content=str(index))
            for index in range(25)
        ]
        await asyncio.gather(*(box.send(message) for message in concurrent))
        drained = await box.drain("bob")
        assert {message.id for message in drained} == {message.id for message in concurrent}, (
            "concurrent sends must not drop messages"
        )
        by_id = {message.id: message for message in concurrent}
        assert all(message == by_id[message.id] for message in drained), (
            "concurrent sends must preserve all message fields"
        )

        once = [
            MailboxMessage(sender="alice", recipient="bob", content=f"once-{index}")
            for index in range(10)
        ]
        for message in once:
            await box.send(message)
        first, second = await asyncio.gather(box.drain("bob"), box.drain("bob"))
        drained_messages = first + second
        drained_ids = [message.id for message in drained_messages]
        assert sorted(drained_ids) == sorted(message.id for message in once), (
            "concurrent drains must deliver each message exactly once"
        )
        by_id = {message.id: message for message in once}
        assert all(message == by_id[message.id] for message in drained_messages), (
            "concurrent drains must preserve all message fields"
        )
    finally:
        await _maybe_close(box)


async def assert_tool_contract(
    tool: Any,
    *,
    valid_input: dict[str, Any],
    ctx: ToolContext | None = None,
    invalid_input: dict[str, Any] | None = None,
) -> ToolResult:
    """Assert the core behavioral contract for a ``Tool`` implementation.

    The helper validates the metadata and callable shape Linch's registry and
    scheduler depend on, executes one known-good call, and returns the
    ``ToolResult`` so adapter tests can make tool-specific assertions.

    Args:
        tool: Tool implementation to validate and execute.
        valid_input: Input payload that the tool must accept.
        ctx: Optional execution context; a minimal contract context is used
            when omitted.
        invalid_input: Optional payload that the tool must reject.

    Returns:
        The result returned by executing ``tool`` with ``valid_input``.

    Raises:
        AssertionError: If metadata, validation, execution, or result shape
            violates the tool contract.
    """

    assert _non_empty_str(getattr(tool, "name", None)), "tool.name must be a non-empty string"
    assert _non_empty_str(getattr(tool, "description", None)), (
        "tool.description must be a non-empty string"
    )

    input_schema = getattr(tool, "input_schema", getattr(tool, "schema", None))
    assert isinstance(input_schema, dict), "tool.input_schema must be a dict"

    scope = getattr(tool, "scope", None)
    assert scope in {"read", "write", "exec"}, "tool.scope must be 'read', 'write', or 'exec'"

    validated = tool.validate(valid_input)
    assert isinstance(validated, dict), "tool.validate(...) must return a dict"

    if invalid_input is not None:
        try:
            tool.validate(invalid_input)
        except Exception:
            pass
        else:
            raise AssertionError("tool.validate(...) must reject invalid_input")

    summary = tool.summarize(validated)
    assert isinstance(summary, str), "tool.summarize(...) must return a string"

    parallel = getattr(tool, "parallel", None)
    assert parallel is not None, "tool.parallel must be a bool or callable"
    if callable(parallel):
        parallel_value = parallel(validated)
        assert isinstance(parallel_value, bool), "tool.parallel(input) must return a bool"
    else:
        assert isinstance(parallel, bool), "tool.parallel must be a bool or callable"

    resources = getattr(tool, "resources", None)
    if callable(resources):
        _assert_resource_accesses(resources(validated), tool_name=str(tool.name))

    context = ctx or ToolContext(
        cwd=".",
        session_id="contract-session",
        run_id="contract-run",
        session_store=None,
    )
    result = await _maybe_await(tool.execute(validated, context))
    assert isinstance(result, ToolResult), "tool.execute(...) must return ToolResult"
    assert isinstance(result.content, str), "ToolResult.content must be a string"
    assert isinstance(result.summary, str), "ToolResult.summary must be a string"
    assert isinstance(result.is_error, bool), "ToolResult.is_error must be a bool"
    assert isinstance(result.metadata, dict), "ToolResult.metadata must be a dict"
    assert isinstance(result.citations, list), "ToolResult.citations must be a list"
    assert isinstance(result.attachments, list), "ToolResult.attachments must be a list"
    assert isinstance(result.duration_ms, int), "ToolResult.duration_ms must be an int"
    assert isinstance(result.truncated, bool), "ToolResult.truncated must be a bool"
    assert isinstance(result.recovery_hint, str), "ToolResult.recovery_hint must be a string"
    return result


async def assert_provider_contract(
    factory: Callable[[], Any | Awaitable[Any]],
    *,
    model: str,
) -> None:
    """Assert the uniform lifecycle/declarative contract for a provider.

    Covers the parts every provider must satisfy without a live transport: a
    non-empty ``id``, a positive ``context_window(model)``, a well-formed
    ``ProviderCapabilities`` whose ``context_window`` agrees with
    ``context_window(model)``, an optional ``prepare()`` that is callable, and an
    optional transport close (``aclose``/``close``) that is idempotent and never
    raises when the provider never streamed.

    It deliberately does not drive ``stream()`` — event bracketing, cancellation,
    and vendor request-shape (tool choice, structured output, prompt caching)
    depend on a live or faked backend and belong in provider-specific tests.

    Args:
        factory: Zero-arg callable (sync or async) returning the provider.
        model: A model id the provider recognizes, used for the window/capability
            probes.

    Raises:
        AssertionError: If the provider violates the lifecycle or declarative
            contract.
    """
    provider = await _call_factory(factory)
    try:
        assert _non_empty_str(getattr(provider, "id", None)), "provider.id must be a non-empty str"

        window = provider.context_window(model)
        assert isinstance(window, int) and window > 0, (
            "context_window(model) must be a positive int"
        )

        caps = provider.capabilities(model)
        for name in (
            "parallel_tool_calls",
            "structured_output",
            "structured_output_terminal_tool",
            "tool_choice",
            "prompt_cache",
        ):
            assert isinstance(getattr(caps, name), bool), f"capabilities.{name} must be a bool"
        assert isinstance(caps.context_window, int) and caps.context_window > 0, (
            "capabilities.context_window must be a positive int"
        )
        assert caps.context_window == window, (
            "capabilities.context_window must match context_window(model)"
        )

        prepare = getattr(provider, "prepare", None)
        assert prepare is None or callable(prepare), "provider.prepare must be callable if present"

        # Close is optional and must be idempotent (and safe before any stream()).
        await _maybe_close(provider)
        await _maybe_close(provider)
    finally:
        await _maybe_close(provider)


async def _call_factory(factory: Callable[[], T | Awaitable[T]]) -> T:
    value = factory()
    if inspect.isawaitable(value):
        return await value
    return value


async def _maybe_await(value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


async def _maybe_close(adapter: Any) -> None:
    close = getattr(adapter, "aclose", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result
        return
    close = getattr(adapter, "close", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result


async def _release_ignore(backend: IsolationBackend, cwd: str) -> None:
    try:
        await backend.release(cwd)
    except Exception:
        pass


async def _assert_raises(expected: type[BaseException], awaitable: Awaitable[Any]) -> None:
    try:
        await awaitable
    except expected:
        return
    except BaseException as exc:
        raise AssertionError(f"expected {expected.__name__}, got {type(exc).__name__}") from exc
    raise AssertionError(f"expected {expected.__name__}")


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _assert_resource_accesses(raw: Any, *, tool_name: str) -> None:
    if raw is None:
        return
    items = [raw] if isinstance(raw, ResourceAccess) else raw
    assert isinstance(items, list | tuple), (
        f"{tool_name}.resources(...) must return ResourceAccess, a list/tuple, or None"
    )
    for item in items:
        if isinstance(item, ResourceAccess):
            assert _non_empty_str(item.resource), "ResourceAccess.resource must be non-empty"
            assert item.mode in {"read", "write"}, "ResourceAccess.mode must be read or write"
            continue
        assert isinstance(item, dict), "resources entries must be ResourceAccess or dict"
        resource = item.get("resource")
        mode = item.get("mode", "read")
        assert _non_empty_str(resource), "resource dict must include non-empty resource"
        assert mode in {"read", "write"}, "resource dict mode must be read or write"

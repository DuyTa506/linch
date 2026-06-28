from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from ..coordination.mailbox import Mailbox, MailboxMessage
from ..coordination.scheduling import ClaimingScheduleStore, Schedule, ScheduleStore
from ..filesystem import FileBackend
from ..memory import MemoryItem, MemoryStore
from ..tools.base import ResourceAccess, ToolContext, ToolResult
from ..tools.isolation import IsolationBackend

T = TypeVar("T")


async def assert_file_backend_contract(
    factory: Callable[[], FileBackend | Awaitable[FileBackend]],
) -> None:
    """Assert the core behavioral contract for a ``FileBackend`` implementation."""

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
    """Assert the core behavioral contract for an ``IsolationBackend`` implementation."""

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
    """

    box = await _call_factory(factory)
    try:
        assert await box.drain("missing") == [], "drain of an unknown recipient must be empty"

        isolated = MailboxMessage(sender="alice", recipient="bob", content="for-bob")
        await box.send(isolated)
        assert await box.drain("carol") == [], "drain must be isolated per recipient"
        drained = await box.drain("bob")
        assert [message.id for message in drained] == [isolated.id], "sent message was not drained"
        assert await box.drain("bob") == [], "drain must be destructive"

        fifo = [
            MailboxMessage(sender="alice", recipient="bob", content=str(index))
            for index in range(5)
        ]
        for message in fifo:
            await box.send(message)
        drained = await box.drain("bob")
        assert {message.id for message in drained} == {message.id for message in fifo}, (
            "drain must return all sequentially sent messages exactly once"
        )
        assert len(drained) == len(fifo), "drain must not duplicate sequential messages"

        concurrent = [
            MailboxMessage(sender="alice", recipient="bob", content=str(index))
            for index in range(25)
        ]
        await asyncio.gather(*(box.send(message) for message in concurrent))
        drained = await box.drain("bob")
        assert {message.id for message in drained} == {message.id for message in concurrent}, (
            "concurrent sends must not drop messages"
        )

        once = [
            MailboxMessage(sender="alice", recipient="bob", content=f"once-{index}")
            for index in range(10)
        ]
        for message in once:
            await box.send(message)
        first, second = await asyncio.gather(box.drain("bob"), box.drain("bob"))
        drained_ids = [message.id for message in first + second]
        assert sorted(drained_ids) == sorted(message.id for message in once), (
            "concurrent drains must deliver each message exactly once"
        )
    finally:
        await _maybe_close(box)


async def assert_memory_store_contract(
    factory: Callable[[], MemoryStore | Awaitable[MemoryStore]],
) -> None:
    """Assert the core behavioral contract for a ``MemoryStore`` implementation."""

    store = await _call_factory(factory)
    try:
        assert await store.search("anything", namespace="docs") == [], (
            "fresh store must return no search results"
        )
        assert await store.search("", namespace="docs") == [], "empty query must return no results"
        assert await store.search("anything", namespace="docs", limit=0) == [], (
            "limit=0 must return no results"
        )

        await store.upsert(
            [
                MemoryItem(
                    id="alpha",
                    content="alpha shared memory",
                    metadata={"kind": "guide"},
                    namespace="docs",
                ),
                MemoryItem(
                    id="beta",
                    content="alpha private memory",
                    metadata={"kind": "note"},
                    namespace="private",
                ),
                MemoryItem(
                    id="gamma",
                    content="gamma unrelated",
                    metadata={"kind": "guide"},
                    namespace="docs",
                ),
            ]
        )

        hits = await store.search("alpha memory", namespace="docs")
        assert [hit.item.id for hit in hits] == ["alpha"], (
            "namespace filtering must exclude other namespaces"
        )
        assert hits[0].item.namespace == "docs"
        assert hits[0].item.metadata == {"kind": "guide"}
        assert hits[0].score is None or isinstance(hits[0].score, int | float)

        private_hits = await store.search("alpha memory", namespace="private")
        assert [hit.item.id for hit in private_hits] == ["beta"]

        filtered = await store.search(
            "alpha memory",
            namespace="docs",
            metadata_filter={"kind": "guide"},
        )
        assert [hit.item.id for hit in filtered] == ["alpha"]
        assert (
            await store.search("alpha memory", namespace="docs", metadata_filter={"kind": "note"})
        ) == []

        await store.upsert(
            [
                MemoryItem(
                    id="alpha",
                    content="replacement delta memory",
                    metadata={"kind": "replacement"},
                    namespace="docs",
                )
            ]
        )
        assert await store.search("shared", namespace="docs") == [], (
            "upsert must replace an existing item with the same namespace and id"
        )
        replaced = await store.search("replacement delta", namespace="docs")
        assert [hit.item.id for hit in replaced] == ["alpha"]
        assert replaced[0].item.metadata == {"kind": "replacement"}

        await store.upsert(
            [
                MemoryItem(id=f"limit-{index}", content="limit token", namespace="docs")
                for index in range(3)
            ]
        )
        limited = await store.search("limit token", namespace="docs", limit=1)
        assert len(limited) == 1, "search must honor limit"

        concurrent = [
            MemoryItem(id=f"concurrent-{index}", content="concurrent token", namespace="docs")
            for index in range(10)
        ]
        await asyncio.gather(*(store.upsert([item]) for item in concurrent))
        concurrent_hits = await store.search("concurrent", namespace="docs", limit=20)
        assert {hit.item.id for hit in concurrent_hits} == {item.id for item in concurrent}, (
            "concurrent upserts must not drop items"
        )
    finally:
        await _maybe_close(store)


async def assert_schedule_store_contract(
    factory: Callable[[], ScheduleStore | Awaitable[ScheduleStore]],
) -> None:
    """Assert the core behavioral contract for a ``ScheduleStore`` implementation.

    Stores that also implement ``claim_due`` are checked for the atomic claim
    invariant used by ``SchedulerLoop``.
    """

    store = await _call_factory(factory)
    try:
        assert await store.list() == [], "fresh store must list no schedules"

        schedule = Schedule(id="contract-interval", payload="alpha", interval_s=60)
        schedule.next_run = 1000.0
        await store.add(schedule)

        loaded = await store.get(schedule.id)
        assert loaded is not None, "added schedule must be retrievable"
        assert loaded.id == schedule.id
        assert loaded.payload == "alpha"
        assert [item.id for item in await store.list()] == [schedule.id]

        updated = Schedule(
            id=schedule.id,
            payload="beta",
            interval_s=30,
            next_run=2000.0,
            enabled=False,
            metadata={"kind": "contract"},
        )
        await store.update(updated)
        loaded = await store.get(schedule.id)
        assert loaded is not None, "updated schedule must remain retrievable"
        assert loaded.payload == "beta"
        assert loaded.interval_s == 30
        assert loaded.next_run == 2000.0
        assert loaded.enabled is False
        assert loaded.metadata == {"kind": "contract"}

        assert await store.remove(schedule.id) is True
        assert await store.remove(schedule.id) is False
        assert await store.get(schedule.id) is None
        assert await store.list() == []

        if isinstance(store, ClaimingScheduleStore):
            due = Schedule(id="contract-due", payload="due", interval_s=60, next_run=1000.0)
            future = Schedule(
                id="contract-future",
                payload="future",
                interval_s=60,
                next_run=5000.0,
            )
            disabled = Schedule(
                id="contract-disabled",
                payload="disabled",
                interval_s=60,
                next_run=1000.0,
                enabled=False,
            )
            await store.add(due)
            await store.add(future)
            await store.add(disabled)

            first, second = await asyncio.gather(store.claim_due(1000.0), store.claim_due(1000.0))
            claimed_ids = [schedule.id for schedule in first + second]
            assert claimed_ids == [due.id], "claim_due must atomically claim each due schedule once"

            loaded_due = await store.get(due.id)
            assert loaded_due is not None
            assert loaded_due.next_run == 1060.0, "claim_due must advance next_run before returning"
            assert await store.get(future.id) is not None
            assert await store.get(disabled.id) is not None
    finally:
        await _maybe_close(store)


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

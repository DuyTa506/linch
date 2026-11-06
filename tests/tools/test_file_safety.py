"""File tools reject stale read evidence and keep blocking work off the loop."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from linch.errors import ToolExecutionError
from linch.tools.base import ToolContext
from linch.tools.builtin import BashTool, EditTool, ReadTool, WriteTool
from linch.tools.file_tracker import FileReadTracker


def context(root: Path) -> ToolContext:
    return ToolContext(
        cwd=str(root),
        session_id="s",
        run_id="r",
        session_store=None,
        file_read_tracker=FileReadTracker(),
    )


async def change(tool, ctx, content="updated"):
    payload = (
        {"file_path": "a.txt", "content": content}
        if tool.name == "Write"
        else {
            "file_path": "a.txt",
            "old_string": "old",
            "new_string": content,
        }
    )
    return await tool.execute(payload, ctx)


@pytest.mark.parametrize("tool", [WriteTool(), EditTool()])
async def test_external_change_requires_reread(tmp_path, tool):
    target = tmp_path / "a.txt"
    target.write_text("old")
    ctx = context(tmp_path)
    await ReadTool().execute({"file_path": "a.txt"}, ctx)
    target.write_text("old plus external change")
    result = await change(tool, ctx)
    assert result.is_error
    assert "Read" in result.content
    assert target.read_text() == "old plus external change"


async def test_bash_sed_change_requires_reread(tmp_path):
    (tmp_path / "a.txt").write_text("old")
    ctx = context(tmp_path)
    await ReadTool().execute({"file_path": "a.txt"}, ctx)
    await BashTool().execute({"command": "sed -i 's/old/old external/' a.txt"}, ctx)
    assert (await change(EditTool(), ctx)).is_error


async def test_new_write_needs_no_read_but_existing_write_does(tmp_path):
    ctx = context(tmp_path)
    assert not (await change(WriteTool(), ctx, "old")).is_error
    fresh = context(tmp_path)
    assert (await change(WriteTool(), fresh)).is_error
    assert (tmp_path / "a.txt").read_text() == "old"


@pytest.mark.parametrize("tool", [WriteTool(), EditTool()])
async def test_missing_stamp_requires_actual_read(tmp_path, tool):
    (tmp_path / "a.txt").write_text("old")
    ctx = context(tmp_path)
    ctx.file_read_tracker.mark_read(str(tmp_path / "a.txt"))
    assert (await change(tool, ctx)).is_error


async def test_own_writes_refresh_stamp(tmp_path):
    (tmp_path / "a.txt").write_text("old")
    ctx = context(tmp_path)
    await ReadTool().execute({"file_path": "a.txt"}, ctx)
    assert not (await change(WriteTool(), ctx, "old extended")).is_error
    assert not (await change(EditTool(), ctx, "new")).is_error
    assert not (await change(WriteTool(), ctx, "last")).is_error


@pytest.mark.parametrize("original_window", [False, True])
async def test_changed_file_partial_reread_drops_old_evidence(tmp_path, original_window):
    target = tmp_path / "a.txt"
    target.write_text("old\nsecond\nthird")
    ctx = context(tmp_path)
    window = {"offset": 1, "limit": 1} if original_window else {}
    await ReadTool().execute({"file_path": "a.txt", **window}, ctx)
    target.write_text("old\nsecond changed\nthird")
    await ReadTool().execute({"file_path": "a.txt", "offset": 2, "limit": 1}, ctx)
    assert (await change(EditTool(), ctx)).is_error
    assert (await change(WriteTool(), ctx)).is_error


async def test_stamps_survive_json_snapshot_and_detect_change(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("old")
    ctx = context(tmp_path)
    await ReadTool().execute({"file_path": "a.txt"}, ctx)
    snapshot = json.loads(json.dumps(ctx.file_read_tracker.snapshot()))
    ctx.file_read_tracker = FileReadTracker()
    ctx.file_read_tracker.restore(snapshot)
    target.write_text("old changed")
    assert (await change(EditTool(), ctx)).is_error


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        [],
        {},
        {"files": ["x"]},
        {
            "files": ["x", 1],
            "ranges": {"x": [None, [1], ["bad", 2], [1, 2]]},
            "stamps": {"x": ["bad", 3], "y": [1, 2, 3]},
        },
    ],
)
def test_tracker_restore_tolerates_old_or_malformed_snapshots(snapshot):
    tracker = FileReadTracker()
    tracker.restore(snapshot)
    assert not tracker.is_current("x", (1, 2))


async def test_large_read_fails_before_loading_bytes(tmp_path, monkeypatch):
    target = tmp_path / "a.txt"
    target.write_bytes(b"a" * (10 * 1024 * 1024 + 1))

    def forbidden(*args, **kwargs):
        raise AssertionError("oversized file must not be opened")

    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(ToolExecutionError, match="Grep|offset"):
        await ReadTool().execute({"file_path": "a.txt"}, context(tmp_path))


async def test_configured_read_limit_and_binary_read(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"text")
    with pytest.raises(ToolExecutionError, match="Grep|offset"):
        await ReadTool(max_read_bytes=3).execute({"file_path": "a.txt"}, context(tmp_path))
    (tmp_path / "a.txt").write_bytes(b"binary\x00text")
    with pytest.raises(ToolExecutionError, match="[Bb]inary"):
        await ReadTool().execute({"file_path": "a.txt"}, context(tmp_path))


@pytest.mark.parametrize("tool", [ReadTool(), WriteTool(), EditTool()])
async def test_file_operations_leave_event_loop_responsive(tmp_path, monkeypatch, tool):
    target = tmp_path / "a.txt"
    target.write_text("old\n" * 100_000)
    ctx = context(tmp_path)
    await ReadTool().execute({"file_path": "a.txt", "limit": -1}, ctx)
    original = Path.resolve
    event_thread = threading.get_ident()

    def slow_resolve(path, *args, **kwargs):
        time.sleep(0.05)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", slow_resolve)
    ticks = 0
    running = True

    async def heartbeat():
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0.005)

    pulse = asyncio.create_task(heartbeat())
    try:
        if tool.name == "Read":
            await tool.execute({"file_path": "a.txt", "limit": -1}, ctx)
        else:
            await change(tool, ctx)
    finally:
        running = False
        await pulse
    assert ticks >= 5, f"file operation blocked event loop thread {event_thread}"


@pytest.mark.parametrize("changed", [False, True])
async def test_durable_suspended_run_restores_file_evidence(tmp_path, changed):
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn

    target = tmp_path / "a.txt"
    target.write_text("old")
    sessions, runs = InMemorySessionStore(), InMemoryRunStore()

    def agent(provider):
        return make_agent(
            provider,
            cwd=str(tmp_path),
            session_store=sessions,
            run_store=runs,
            tools=empty_tools(ReadTool(), EditTool()),
        )

    first = agent(
        ScriptedProvider(
            [
                ToolUseTurn("Read", {"file_path": "a.txt"}, tool_id="read"),
                ToolUseTurn(
                    "Edit",
                    {
                        "file_path": "a.txt",
                        "old_string": "old",
                        "new_string": "updated",
                    },
                    tool_id="edit",
                ),
            ]
        )
    )
    session = await first.session(id="file-resume")
    iterator = session.run("read then edit").__aiter__()
    run_id = ""
    try:
        async for event in iterator:
            if event.type == "system":
                run_id = event.run_id
            if event.type == "assistant" and any(
                getattr(block, "name", None) == "Edit" for block in event.message.content
            ):
                break
    finally:
        await iterator.aclose()
    stored = await runs.load_run(run_id)
    assert stored is not None and stored.checkpoint is not None
    assert stored.checkpoint.phase == "assistant_appended"
    if changed:
        target.write_text("old external")
    restarted = agent(ScriptedProvider([TextTurn("done")]))
    resumed = await restarted.session(id="file-resume")
    events = [event async for event in resumed.resume(run_id)]
    results = [event for event in events if event.type == "tool_call_end"]
    assert results
    assert results[0].is_error is changed
    assert target.read_text() == ("old external" if changed else "updated")
    await first.close()
    await restarted.close()


async def test_read_stamp_cannot_bless_a_change_during_content_read(tmp_path, monkeypatch):
    target = tmp_path / "a.txt"
    target.write_text("old")
    original = Path.open

    class ChangingFile:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

        def read(self, *args):
            data = self.file.read(*args)
            target.write_text("old external")
            return data

        def __getattr__(self, name):
            return getattr(self.file, name)

    def changing_open(path, mode="r", *args, **kwargs):
        file = original(path, mode, *args, **kwargs)
        return ChangingFile(file) if mode == "rb" else file

    monkeypatch.setattr(Path, "open", changing_open)
    ctx = context(tmp_path)
    try:
        await ReadTool().execute({"file_path": "a.txt"}, ctx)
    except ToolExecutionError:
        pass
    assert (await change(WriteTool(), ctx)).is_error


async def test_same_size_external_change_and_reread(tmp_path):
    import os

    target = tmp_path / "a.txt"
    target.write_text("old")
    ctx = context(tmp_path)
    await ReadTool().execute({"file_path": "a.txt"}, ctx)
    timestamp = target.stat().st_mtime_ns
    target.write_text("new")
    os.utime(target, ns=(timestamp + 1_000_000, timestamp + 1_000_000))
    assert (await change(WriteTool(), ctx)).is_error
    await ReadTool().execute({"file_path": "a.txt"}, ctx)
    assert not (await change(WriteTool(), ctx)).is_error


async def test_unstamped_hook_mark_preserves_real_read_stamp(tmp_path):
    (tmp_path / "a.txt").write_text("old")
    ctx = context(tmp_path)
    await ReadTool().execute({"file_path": "a.txt"}, ctx)
    ctx.file_read_tracker.mark_read(str(tmp_path / "a.txt"))
    assert not (await change(EditTool(), ctx)).is_error


async def test_read_rejects_growth_between_stat_and_open(tmp_path, monkeypatch):
    target = tmp_path / "a.txt"
    target.write_text("old")
    original = Path.open

    def growing_open(path, mode="r", *args, **kwargs):
        if mode == "rb":
            target.write_text("now oversized")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", growing_open)
    with pytest.raises(ToolExecutionError, match="Grep"):
        await ReadTool(max_read_bytes=3).execute({"file_path": "a.txt"}, context(tmp_path))


async def test_read_at_exact_byte_limit_preserves_line_window(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"a\r\nb\n")
    result = await ReadTool(max_read_bytes=5).execute(
        {"file_path": "a.txt", "offset": 2, "limit": 1}, context(tmp_path)
    )
    assert result.content == "2\tb"


@pytest.mark.parametrize("invalid", [0, -1, True, 2.5])
def test_invalid_read_cap_rejected(invalid):
    with pytest.raises(ValueError, match="positive integer"):
        ReadTool(max_read_bytes=invalid)


async def test_read_rejects_changed_version_before_granting_evidence(tmp_path, monkeypatch):
    target = tmp_path / "a.txt"
    target.write_text("old")
    original = Path.open

    class ChangingFile:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

        def read(self, *args):
            data = self.file.read(*args)
            target.write_text("old external")
            return data

        def __getattr__(self, name):
            return getattr(self.file, name)

    def changing_open(path, mode="r", *args, **kwargs):
        file = original(path, mode, *args, **kwargs)
        return ChangingFile(file) if mode == "rb" else file

    monkeypatch.setattr(Path, "open", changing_open)
    ctx = context(tmp_path)
    with pytest.raises(ToolExecutionError, match="Read.*again"):
        await ReadTool().execute({"file_path": "a.txt"}, ctx)
    assert not ctx.file_read_tracker.has_read(str(target))


async def test_large_file_read_io_keeps_heartbeat_running(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_bytes(b"x" * (9 * 1024 * 1024))
    original = Path.open

    class SlowFile:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

        def read(self, *args):
            time.sleep(0.08)
            return self.file.read(*args)

        def __getattr__(self, name):
            return getattr(self.file, name)

    def slow_open(path, mode="r", *args, **kwargs):
        file = original(path, mode, *args, **kwargs)
        return SlowFile(file) if mode == "rb" else file

    monkeypatch.setattr(Path, "open", slow_open)
    operation = asyncio.create_task(ReadTool().execute({"file_path": "a.txt"}, context(tmp_path)))
    ticks = 0
    while not operation.done():
        await asyncio.sleep(0.005)
        ticks += 1
    assert not (await operation).is_error
    assert ticks >= 5


@pytest.mark.parametrize("tool", [WriteTool(), EditTool()])
@pytest.mark.parametrize("repeat_cancel", [False, True])
@pytest.mark.parametrize("worker_error", [False, True])
async def test_cancelled_mutation_settles_worker_before_returning(
    tmp_path, monkeypatch, tool, repeat_cancel, worker_error
):
    from linch.tools import builtin

    target = tmp_path / "a.txt"
    target.write_text("old")
    ctx = context(tmp_path)
    await ReadTool().execute({"file_path": "a.txt"}, ctx)
    entered, release = threading.Event(), threading.Event()
    original = builtin._atomic_write

    def blocked_write(path, content):
        entered.set()
        assert release.wait(2)
        if worker_error:
            raise OSError("worker failed during cancellation")
        original(path, content)

    monkeypatch.setattr(builtin, "_atomic_write", blocked_write)
    mutation = asyncio.create_task(change(tool, ctx, "first"))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    try:
        mutation.cancel()
        await asyncio.sleep(0.01)
        if repeat_cancel:
            mutation.cancel()
            await asyncio.sleep(0.01)
        assert not mutation.done(), "cancel returned while a mutation worker was still active"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await mutation
    monkeypatch.setattr(builtin, "_atomic_write", original)
    assert not (await change(WriteTool(), ctx, "second")).is_error
    assert target.read_text() == "second"


async def test_mutation_timeout_waits_for_worker_before_next_serial_write(tmp_path, monkeypatch):
    from linch.tools import builtin

    target = tmp_path / "a.txt"
    target.write_text("old")
    ctx = context(tmp_path)
    await ReadTool().execute({"file_path": "a.txt"}, ctx)
    entered, release = threading.Event(), threading.Event()
    original = builtin._atomic_write

    def blocked_write(path, content):
        if content == "first":
            entered.set()
            assert release.wait(2)
        original(path, content)

    monkeypatch.setattr(builtin, "_atomic_write", blocked_write)

    async def serialized_writes():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(change(WriteTool(), ctx, "first"), timeout=0.01)
        assert not (await change(WriteTool(), ctx, "second")).is_error

    writes = asyncio.create_task(serialized_writes())
    while not entered.is_set():
        await asyncio.sleep(0.001)
    try:
        await asyncio.sleep(0.03)
        assert not writes.done(), "timeout released serialization before mutation settled"
    finally:
        release.set()
        await writes
    assert target.read_text() == "second"


async def test_parallel_reads_cannot_mix_versions_and_whole_file_coverage(tmp_path, monkeypatch):
    target = tmp_path / "a.txt"
    target.write_text("old one\nold two\n")
    ctx = context(tmp_path)
    entered, release = threading.Event(), threading.Event()
    event_thread = threading.get_ident()
    original = ctx.file_read_tracker.set_stamp
    first_publication = True

    def pause_first_worker_publication(path, stamp, *, reset=False):
        nonlocal first_publication
        original(path, stamp, reset=reset)
        if threading.get_ident() != event_thread and first_publication:
            first_publication = False
            entered.set()
            assert release.wait(2)

    monkeypatch.setattr(ctx.file_read_tracker, "set_stamp", pause_first_worker_publication)
    full_read = asyncio.create_task(ReadTool().execute({"file_path": "a.txt"}, ctx))
    while not entered.is_set() and not full_read.done():
        await asyncio.sleep(0.001)
    changed = "external one\nexternal two\n"
    try:
        target.write_text(changed)
        partial = await ReadTool().execute({"file_path": "a.txt", "offset": 2, "limit": 1}, ctx)
        assert partial.content == "2\texternal two"
    finally:
        release.set()
        assert (await full_read).content == "1\told one\n2\told two\n3\t"
    result = await change(WriteTool(), ctx, "unsafe overwrite")
    assert result.is_error
    assert target.read_text() == changed


async def test_cancelled_read_cannot_publish_late_write_authority(tmp_path, monkeypatch):
    target = tmp_path / "a.txt"
    target.write_text("old")
    ctx = context(tmp_path)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = ReadTool._execute

    def blocked_read(tool, input, context):
        entered.set()
        assert release.wait(2)
        try:
            return original(tool, input, context)
        finally:
            finished.set()

    monkeypatch.setattr(ReadTool, "_execute", blocked_read)
    read = asyncio.create_task(ReadTool().execute({"file_path": "a.txt"}, ctx))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read
    release.set()
    while not finished.is_set():
        await asyncio.sleep(0.001)
    assert (await change(WriteTool(), ctx)).is_error
    assert target.read_text() == "old"

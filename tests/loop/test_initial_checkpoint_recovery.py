"""Startup takeover across actual durable store boundaries, with fresh runtimes."""

from __future__ import annotations

import copy
import inspect
import re
from uuid import uuid4

import pytest
from test_run_resume import ScriptProvider

from linch import Agent, DurabilityOptions, HookResult, RunOptions, empty_tools
from linch.events import event_to_dict
from linch.run_store import InMemoryRunStore, LeaseLostError
from linch.run_stores.postgres import PostgresRunStore
from linch.sessions import InMemorySessionStore
from linch.sessions.postgres import PostgresSessionStore
from linch.types import Message, TextBlock


class ProcessDeath(BaseException):
    pass


class Boundaries:
    """A dead process cannot flush events from a Python generator's finally."""

    def __init__(self, target=None, timing="before"):
        self.target, self.timing = target, timing
        self.calls = []
        self.dead = False
        self.active = False
        self.finished = False

    def wrap(self, store, label):
        state = self

        class Handle:
            def __getattr__(self, name):
                method = getattr(store, name)
                if not inspect.iscoroutinefunction(method):
                    return method

                async def call(*args, **kwargs):
                    if state.dead:
                        raise ProcessDeath()
                    if name == "create_run":
                        state.active = True
                    selected = state.active and not state.finished and name != "close"
                    phase = getattr(args[1], "phase", "") if len(args) > 1 else ""
                    key = (label, name, phase)
                    index = len(state.calls)
                    if selected:
                        state.calls.append(key)
                    hit = selected and index == state.target
                    if hit and state.timing == "before":
                        state.dead = True
                        raise ProcessDeath()
                    result = await method(*args, **kwargs)
                    if hit:
                        state.dead = True
                        if state.timing == "committed":
                            raise ProcessDeath()
                        # The caller gets the return value (including SessionLog's
                        # append); the next boundary observes sticky process death.
                    if selected and phase == "provider_pending":
                        state.finished = True
                    return result

                return call

        return Handle()


class Transform:
    resume_policy_id = "startup-transform"
    resume_policy_version = "1"
    resume_policy_config = {}
    checkpoint_key = "test.transform"

    def __init__(self):
        self.starts = 0

    def on_agent_start(self, ctx):
        self.starts += 1

    def on_user_prompt_submit(self, ctx):
        return HookResult.mutate(prompt=ctx.prompt + f" rewritten {self.starts}")

    def checkpoint_state(self, session, run_id):
        return {"starts": self.starts}

    def restore_checkpoint_state(self, state, session, run_id):
        self.starts = state["starts"]


def normalized(rows):
    identities = {}

    def visit(value, key=""):
        if isinstance(value, dict):
            return {k: visit(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v, key) for v in value]
        if key in {"duration_ms", "timestamp", "created_at", "started_at", "completed_at"}:
            return "<time>"
        if isinstance(value, str) and (
            key == "projection_id"
            or re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", value)
        ):
            return identities.setdefault(value, f"id-{len(identities)}")
        return value

    return visit(
        [event_to_dict(row.event) for row in rows if row.event.type != "prompt_cache_advisory"]
    )


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("variant", ["basic", "exact", "transform-identical-tail"])
async def test_startup_store_boundary_takeover_trace(kind, variant, pg_dsn):
    async def exercise(target=None, timing="before"):
        if kind == "memory":
            raw = InMemoryRunStore()
            messages = InMemorySessionStore()
        else:
            raw = PostgresRunStore(pg_dsn)
            messages = PostgresSessionStore(pg_dsn)
        gate = Boundaries(target, timing)
        provider = ScriptProvider()
        rid, sid = str(uuid4()), str(uuid4())

        def make(store, session_store):
            agent = Agent(
                model="m",
                provider=provider,
                run_store=store,
                session_store=session_store,
                tools=empty_tools(),
                hooks=[Transform()] if variant == "transform-identical-tail" else [],
                durability=DurabilityOptions(exact_model_input=variant == "exact"),
                permissions={"mode": "skip-dangerous"},
                cwd=".",
            )
            if variant == "transform-identical-tail":
                from linch.skills.types import Skill, SkillFrontmatter

                agent.skills = {
                    "test": Skill("test", ".", SkillFrontmatter("test", "fixture"), "body")
                }
            return agent

        first = make(gate.wrap(raw, "run"), gate.wrap(messages, "session"))
        session = await first.session(id=sid)
        if variant == "transform-identical-tail":
            await session.append(
                [Message(role="user", content=[TextBlock(text="hello rewritten 1")])]
            )
        # Pending is the queue's durable row, before SDK adoption.
        if kind == "memory":
            await raw.create_run(sid, id=rid)
            raw._runs[rid].status = "pending"
        else:
            pool = await raw._ensure()
            async with pool.acquire() as conn:
                await raw.create_pending_run(conn, sid, id=rid)
        old = await raw.claim(rid, "old", 30)
        replayed_started = True
        try:
            try:
                _ = [event async for event in session.run("hello", RunOptions(run_id=rid))]
            except ProcessDeath:
                pass
            if target is not None:
                assert gate.dead, (target, gate.calls)
                assert provider.calls == 0
                # Simulate lease expiry without allowing old-owner cleanup writes.
                if kind == "memory":
                    raw._lease_state[rid] = ("old", 0, old.epoch)
                    peer = copy.copy(raw)
                    for name in ("_held_leases", "_lease_ttls", "_lease_owners"):
                        setattr(peer, name, {})
                else:
                    await (await raw._ensure()).execute(
                        "UPDATE runs SET lease_expires_at=0 WHERE id=$1", rid
                    )
                    peer = PostgresRunStore(pg_dsn)
                new = await peer.claim(rid, "new", 30)
                assert new.epoch > old.epoch
                second = make(peer, messages)
                fresh = await second.session(id=sid)
                record = await peer.load_run(rid)
                replayed_started = record.checkpoint is None or record.checkpoint.phase == "started"
                iterator = (
                    fresh.resume(rid)
                    if record.checkpoint
                    else fresh.run("hello", RunOptions(run_id=rid))
                )
                try:
                    _ = [event async for event in iterator]
                    session = fresh
                    with pytest.raises(LeaseLostError):
                        await raw.mark_failed(rid, error={"name": "zombie"})
                finally:
                    await second.close()
            record = await raw.load_run(rid)
            assert record.status == "completed"
            if variant == "transform-identical-tail" and replayed_started:
                assert record.checkpoint.extension_state["test.transform"] == {"starts": 1}
            rows = await raw.load_events(rid)
            assert [row.seq for row in rows] == list(range(1, len(rows) + 1))
            types = [row.event.type for row in rows]
            assert types[0] == "system"
            assert types.count("system") == types.count("user") == types.count("result") == 1
            assert types[-1] == "result"
            assert provider.calls == 1
            history = [m for m in session.full_history if m.role == "user"]
            assert len(history) == (2 if variant == "transform-identical-tail" else 1)
            return (
                gate.calls,
                normalized(rows),
                list(session.full_history),
                list(session.provider_view),
            )
        finally:
            gate.dead = False
            gate.active = False
            await first.close()
            await raw.close()
            await messages.close()

    calls, baseline, history, view = await exercise()
    assert any(phase == "provider_pending" for _, _, phase in calls)
    for target, boundary in enumerate(calls):
        for timing in ("before", "committed", "returned"):
            _, trace, recovered_history, recovered_view = await exercise(target, timing)
            assert trace == baseline, (boundary, timing, trace, baseline)
            assert recovered_history == history, (boundary, timing)
            assert recovered_view == view, (boundary, timing)


@pytest.mark.parametrize("after_event", ["system", "hook"])
async def test_graceful_startup_close_does_not_duplicate_durable_prefix(after_event):
    raw, messages = InMemoryRunStore(), InMemorySessionStore()
    provider = ScriptProvider()

    def make():
        return Agent(
            model="m",
            provider=provider,
            run_store=raw,
            session_store=messages,
            tools=empty_tools(),
            hooks=[Transform()],
            cwd=".",
        )

    first = make()
    session = await first.session(id="s")
    iterator = session.run("hello", RunOptions(run_id="r"))
    async for event in iterator:
        if event.type == after_event:
            break
    await iterator.aclose()
    record = await raw.load_run("r")
    assert record.checkpoint.phase == "started"
    assert await raw.load_events("r") == []
    second = make()
    try:
        fresh = await second.session(id="s")
        _ = [event async for event in fresh.resume("r")]
        rows = await raw.load_events("r")
        assert [r.event.type for r in rows].count("system") == 1
        assert [r.event.type for r in rows].count("hook") == 1
    finally:
        await first.close()
        await second.close()


async def test_same_session_resume_after_skills_yield_keeps_startup_atomic():
    from linch.skills.types import Skill, SkillFrontmatter

    raw = InMemoryRunStore()
    agent = Agent(
        model="m",
        provider=ScriptProvider(),
        run_store=raw,
        session_store=InMemorySessionStore(),
        tools=empty_tools(),
        cwd=".",
    )
    agent.skills = {"test": Skill("test", ".", SkillFrontmatter("test", "fixture"), "body")}
    session = await agent.session(id="s")
    iterator = session.run("hello", RunOptions(run_id="r"))
    try:
        async for event in iterator:
            if event.type == "skills_loaded":
                break
        await iterator.aclose()
        assert not session.skills_loaded_emitted
        assert await raw.load_events("r") == []
        _ = [event async for event in session.resume("r")]
        types = [r.event.type for r in await raw.load_events("r")]
        assert types[:3] == ["system", "skills_loaded", "user"]
        assert types.count("skills_loaded") == 1
        assert session.skills_loaded_emitted
    finally:
        await iterator.aclose()
        await agent.close()


async def test_fresh_prompt_hook_can_append_logged_context_before_prompt():
    class LoggedContext:
        resume_policy_id = "logged-context"

        async def on_user_prompt_submit(self, ctx):
            await ctx.session.append([Message("user", [TextBlock(text="logged context")])])

    agent = Agent(
        model="m",
        provider=ScriptProvider(),
        run_store=InMemoryRunStore(),
        session_store=InMemorySessionStore(),
        tools=empty_tools(),
        hooks=[LoggedContext()],
    )
    try:
        session = await agent.session()
        events = [event async for event in session.run("hello")]
        assert events[-1].subtype == "success"
        assert [m.content[-1].text for m in session.full_history[:2]] == ["logged context", "hello"]
    finally:
        await agent.close()


async def test_initial_hook_checkpoint_receives_run_context_and_option_deps():
    contexts = []

    class ContextHook(Transform):
        def checkpoint_state(self, session, run_id):
            contexts.append((session.active_run_id, run_id, session.run_deps))
            return super().checkpoint_state(session, run_id)

    deps = object()
    agent = Agent(
        model="m",
        provider=ScriptProvider(),
        run_store=InMemoryRunStore(),
        session_store=InMemorySessionStore(),
        tools=empty_tools(),
        hooks=[ContextHook()],
    )
    try:
        session = await agent.session()
        _ = [event async for event in session.run("hello", RunOptions(run_id="r", deps=deps))]
        assert contexts
        assert all(
            active == run_id == "r" and captured is deps for active, run_id, captured in contexts
        )
    finally:
        await agent.close()


@pytest.mark.parametrize("terminal_failure", [False, True])
async def test_blocked_prompt_marks_skills_seen_only_after_terminal_commit(terminal_failure):
    from linch.skills.types import Skill, SkillFrontmatter

    class Store(InMemoryRunStore):
        async def mark_failed(self, *args, **kwargs):
            if terminal_failure:
                raise OSError("terminal unavailable")
            return await super().mark_failed(*args, **kwargs)

    class BlockFirst:
        resume_policy_id = "block-first"
        calls = 0

        def on_user_prompt_submit(self, ctx):
            self.calls += 1
            return HookResult.block("blocked") if self.calls == 1 else None

    store = Store()
    agent = Agent(
        model="m",
        provider=ScriptProvider(),
        run_store=store,
        session_store=InMemorySessionStore(),
        tools=empty_tools(),
        hooks=[BlockFirst()],
    )
    agent.skills = {"test": Skill("test", ".", SkillFrontmatter("test", "fixture"), "body")}
    try:
        session = await agent.session(id="s")
        events = [event async for event in session.run("hello", RunOptions(run_id="blocked"))]
        assert events[-1].type == "result"
        if terminal_failure:
            assert not session.skills_loaded_emitted
            assert await store.load_events("blocked") == []
        else:
            assert session.skills_loaded_emitted
            events = [event async for event in session.run("allowed")]
            assert not any(event.type == "skills_loaded" for event in events)
            assert events[-1].subtype == "success"
    finally:
        await agent.close()

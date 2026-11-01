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
                    events = (
                        args[2]
                        if name == "save_checkpoint_with_events"
                        else (args[1] if name == "append_events" else [])
                    )
                    receipt_hit = isinstance(state.target, tuple) and any(
                        getattr(event, "type", None) == "model_call"
                        and (event.record["delivery_attempt_id"], event.record_kind) == state.target
                        for event in events
                    )
                    hit = selected and (index == state.target or receipt_hit)
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


class AccountedTransform(Transform):
    @staticmethod
    async def paid_action(ctx, delivery_id):
        from linch.model_calls import ModelCallIntent, ModelCallReceipt
        from linch.types import Usage

        # A deterministic fake paid action exercises the runtime's real
        # persistence callback without a network call or duplicate delivery.
        ledger = ctx.session._model_call_ledger
        if ledger.receipt_for(delivery_id) is not None:
            return
        intent = ModelCallIntent(
            delivery_id, delivery_id, None, "primary", ctx.run_id, 0, "m", "startup-request", None
        )
        if ledger.intent_for(delivery_id) is None:
            await ledger.begin(intent)
        if delivery_id not in ledger._started:
            await ledger.mark_dispatch_started(intent)
        if ledger.receipt_for(delivery_id) is None:
            await ledger.settle(
                ModelCallReceipt(
                    delivery_id,
                    delivery_id,
                    "primary",
                    ctx.run_id,
                    0,
                    "m",
                    "reported",
                    Usage(input_tokens=111, output_tokens=22),
                    0.125,
                    "accepted",
                    None,
                    None,
                    None,
                    17,
                    "startup-request",
                    None,
                    None,
                )
            )

    async def on_user_prompt_submit(self, ctx):
        await self.paid_action(ctx, "startup-delivery")
        return super().on_user_prompt_submit(ctx)


class EventAccountedTransform(AccountedTransform):
    async def on_user_prompt_submit(self, ctx):
        from linch.events import HookEventRecord

        result = await super().on_user_prompt_submit(ctx)
        result.metadata["events"] = [
            HookEventRecord(event="UserPromptSubmit", hook="second", action="continue")
        ]
        return result

    async def on_event_emit(self, ctx):
        if ctx.event.type == "hook":
            delivery = "event-second" if ctx.event.hook == "second" else "event-first"
            await self.paid_action(ctx, delivery)


class BlockedErrorAccountedTransform(AccountedTransform):
    def __init__(self):
        super().__init__()
        self.stop_results = []

    def on_agent_stop(self, ctx):
        self.stop_results.append(ctx.result)

    def on_user_prompt_submit(self, ctx):
        return HookResult.block("blocked before provider")

    async def on_event_emit(self, ctx):
        if ctx.event.type == "error":
            await self.paid_action(ctx, "blocked-error")


class LoggedContext:
    """Idempotent logged startup context; no external effects or SDK state edits."""

    resume_policy_id = "startup-logged-context"

    def __init__(self, *, same_as_prompt):
        self.same_as_prompt = same_as_prompt
        self.resume_policy_config = {"same_as_prompt": same_as_prompt}

    async def on_user_prompt_submit(self, ctx):
        from linch.loop.request import build_user_message

        # Both messages include the normal env block. In the equal variant the
        # context and the eventual SDK prompt are fully equal Message values.
        message = build_user_message(ctx.prompt if self.same_as_prompt else "logged context")
        if message not in ctx.session.full_history:
            await ctx.session.append([message])


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
@pytest.mark.parametrize(
    "variant",
    [
        "basic",
        "exact",
        "transform-identical-tail",
        "context-distinct",
        "context-equal",
        "accounted-transform",
        "event-accounted-transform",
        "blocked-error-accounted-transform",
    ],
)
async def test_startup_store_boundary_takeover_trace(kind, variant, pg_dsn):
    blocked = variant == "blocked-error-accounted-transform"
    transforming = variant in {
        "transform-identical-tail",
        "accounted-transform",
        "event-accounted-transform",
    }

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
            hooks = [Transform()] if transforming else []
            if variant == "accounted-transform":
                hooks = [AccountedTransform()]
            elif variant == "event-accounted-transform":
                hooks = [EventAccountedTransform()]
            elif blocked:
                hooks = [BlockedErrorAccountedTransform()]
            if variant.startswith("context-"):
                hooks = [LoggedContext(same_as_prompt=variant == "context-equal")]
            agent = Agent(
                model="m",
                provider=provider,
                run_store=store,
                session_store=session_store,
                tools=empty_tools(),
                hooks=hooks,
                durability=DurabilityOptions(exact_model_input=variant == "exact"),
                permissions={"mode": "skip-dangerous"},
                cwd=".",
            )
            if transforming:
                from linch.skills.types import Skill, SkillFrontmatter

                agent.skills = {
                    "test": Skill("test", ".", SkillFrontmatter("test", "fixture"), "body")
                }
            return agent

        first = make(gate.wrap(raw, "run"), gate.wrap(messages, "session"))
        session = await first.session(id=sid)
        if transforming:
            from linch.loop.request import build_user_message

            await session.append([build_user_message("hello rewritten 1")])
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
                replayed_started = record.checkpoint is None or (
                    record.checkpoint.phase == "started"
                    and "prepared_message"
                    not in record.checkpoint.extension_state.get("linch.startup", {})
                )
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
            assert record.status == ("failed" if blocked else "completed"), (
                target,
                timing,
                gate.calls,
            )
            if transforming and replayed_started:
                assert record.checkpoint.extension_state["test.transform"] == {"starts": 1}
            rows = await raw.load_events(rid)
            assert [row.seq for row in rows] == list(range(1, len(rows) + 1))
            types = [row.event.type for row in rows]
            assert types[0] == "system"
            assert types.count("system") == types.count("result") == 1
            assert types.count("user") == (0 if blocked else 1)
            assert types.count("error") == (1 if blocked else 0)
            assert types[-1] == "result"
            assert provider.calls == (0 if blocked else 1)
            if blocked:
                assert rows[-1].event.subtype == rows[-1].event.stop_reason == "error"
                assert record.checkpoint.total_usage.input_tokens == 111
                assert record.checkpoint.total_usage.output_tokens == 22
                paid = [row.event for row in rows if row.event.type == "model_call"]
                assert [event.record_kind for event in paid] == [
                    "intent",
                    "dispatch_started",
                    "receipt",
                ]
                assert paid[-1].record["cost_usd"] == 0.125
                hook = next(
                    h for h in session.agent.hooks if isinstance(h, BlockedErrorAccountedTransform)
                )
                assert len(hook.stop_results) == 1
                assert hook.stop_results[0].total_usage == record.checkpoint.total_usage
            elif "accounted-transform" in variant:
                paid = [
                    row.event
                    for row in rows
                    if row.event.type == "model_call"
                    and row.event.record["delivery_attempt_id"] == "startup-delivery"
                ]
                assert [event.record_kind for event in paid] == [
                    "intent",
                    "dispatch_started",
                    "receipt",
                ]
                actions = 3 if variant == "event-accounted-transform" else 1
                assert rows[-1].event.total_usage.input_tokens == 111 * actions
                assert rows[-1].event.total_usage.output_tokens == 22 * actions
                assert rows[-1].event.total_cost_usd == 0.125 * actions
                assert rows[-1].event.usage_complete
                if variant == "event-accounted-transform":
                    hook_positions = [i for i, row in enumerate(rows) if row.event.type == "hook"]
                    first_receipt = next(
                        i
                        for i, row in enumerate(rows)
                        if row.event.type == "model_call"
                        and row.event.record_kind == "receipt"
                        and row.event.record["delivery_attempt_id"] == "event-first"
                    )
                    assert hook_positions[0] < first_receipt < hook_positions[1]
            history = [m for m in session.full_history if m.role == "user"]
            assert len(history) == (
                0 if blocked else (2 if transforming or variant.startswith("context-") else 1)
            )
            if variant.startswith("context-"):
                assert history[0].content[-1].text == (
                    "hello" if variant == "context-equal" else "logged context"
                )
                assert history[1].content[-1].text == "hello"
                if variant == "context-equal":
                    assert history[0] == history[1]
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
    assert blocked or any(phase == "provider_pending" for _, _, phase in calls)
    targets = list(enumerate(calls))
    timings = ("before", "committed", "returned")
    if "accounted-transform" in variant:
        # In-flight billable dispatch changes accounting completeness by
        # contract. Test completed durable callback facts, without filtering
        # ModelCallEvent or weakening the full trace/history oracle.
        delivery = (
            "blocked-error"
            if blocked
            else ("event-first" if variant == "event-accounted-transform" else "startup-delivery")
        )
        targets = [((delivery, "receipt"), "completed startup receipt")]
        timings = ("committed", "returned")
    for target, boundary in targets:
        for timing in timings:
            _, trace, recovered_history, recovered_view = await exercise(target, timing)
            assert trace == baseline, (boundary, timing, trace, baseline)
            assert recovered_history == history, (boundary, timing)
            assert recovered_view == view, (boundary, timing)
            if blocked:
                for result in (baseline[-1], trace[-1]):
                    assert result["total_usage"]["input_tokens"] == 111
                    assert result["total_usage"]["output_tokens"] == 22
                    assert result["total_cost_usd"] == 0.125
                    assert result.get("usage_complete", True)


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
    prefix = await raw.load_events("r")
    assert [row.event.type for row in prefix] == (["system"] if after_event == "hook" else [])
    if after_event == "hook":
        assert "prepared_message" in record.checkpoint.extension_state["linch.startup"]
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
        if terminal_failure:
            # A transient terminal-store loss may have emitted the blocked
            # ErrorEvent, but it must not append a durable ResultEvent.
            assert any(event.type == "error" for event in events)
            assert not any(event.type == "result" for event in events)
            assert not session.skills_loaded_emitted
            rows = await store.load_events("blocked")
            assert [row.event.type for row in rows] == ["system", "skills_loaded"]
            assert (await store.load_run("blocked")).status == "running"
        else:
            assert events[-1].type == "result"
            assert session.skills_loaded_emitted
            assert (
                "linch.startup" not in (await store.load_run("blocked")).checkpoint.extension_state
            )
            events = [event async for event in session.run("allowed")]
            assert not any(event.type == "skills_loaded" for event in events)
            assert events[-1].subtype == "success"
    finally:
        await agent.close()


@pytest.mark.parametrize(
    ("handler", "expected_barriers"),
    [
        ("on_hook", 1),
        ("on_agent_start", 1),
        ("on_user_prompt_submit", 1),
        ("on_event_emit", 1),
        ("on_pre_tool_use", 0),
        ("not_callable", 0),
    ],
)
async def test_preparation_barrier_requires_actual_startup_handler(handler, expected_barriers):
    from types import SimpleNamespace

    class Store(InMemoryRunStore):
        barriers = 0

        async def save_checkpoint_with_events(self, run_id, checkpoint, events, **kwargs):
            if checkpoint.phase == "started":
                self.barriers += 1
            return await super().save_checkpoint_with_events(run_id, checkpoint, events, **kwargs)

    hook = SimpleNamespace(resume_policy_id="handler-selection")
    if handler == "not_callable":
        hook.on_user_prompt_submit = "not callable"
    else:
        setattr(hook, handler, lambda *args: None)
    store = Store()
    agent = Agent(
        model="m",
        provider=ScriptProvider(),
        run_store=store,
        session_store=InMemorySessionStore(),
        tools=empty_tools(),
        hooks=[hook],
    )
    try:
        session = await agent.session()
        events = [event async for event in session.run("hello")]
        assert events[-1].subtype == "success"
        assert store.barriers == expected_barriers
    finally:
        await agent.close()


async def test_no_store_blocked_error_callback_refreshes_result_and_stop_usage():
    hook = BlockedErrorAccountedTransform()
    provider = ScriptProvider()
    agent = Agent(
        model="m",
        provider=provider,
        run_store=None,
        session_store=InMemorySessionStore(),
        tools=empty_tools(),
        hooks=[hook],
    )
    try:
        session = await agent.session()
        events = [event async for event in session.run("blocked")]
        assert [event.type for event in events].count("error") == 1
        result = events[-1]
        assert result.type == "result" and result.subtype == "error"
        assert result.total_usage.input_tokens == 111
        assert result.total_usage.output_tokens == 22
        assert result.total_cost_usd == 0.125 and result.usage_complete
        assert len(hook.stop_results) == 1
        assert hook.stop_results[0].total_usage == result.total_usage
        assert provider.calls == 0
    finally:
        await agent.close()

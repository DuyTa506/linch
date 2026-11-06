from __future__ import annotations

from types import SimpleNamespace

import pytest

from linch.session_log import SessionLog
from linch.types import Message, TextBlock


def _session(*, listing: str = "- alpha: Alpha skill") -> SimpleNamespace:
    log = SessionLog()
    agent = SimpleNamespace(
        skill_listing_text=listing,
        skills={},
        model="model",
        tools=SimpleNamespace(),
    )

    class _Session(SimpleNamespace):
        @property
        def provider_view(self):
            return self.session_log.provider_view

        @property
        def full_history(self):
            return self.session_log.full_history

    session = _Session(
        agent=agent,
        session_log=log,
        invoked_skills=(),
        meta={
            "linch.prefix": {
                "version": 1,
                "system": [
                    {
                        "text": "<linch-skill-listing>\n- old: Old skill\n</linch-skill-listing>",
                        "cacheable": True,
                    }
                ],
                "tools": [],
                "base": {"tools": {}, "skills": {}, "subagents": None},
                "notified": {"tools": {}, "skills": {}, "subagents": None},
            }
        },
        tools_override=None,
        system_blocks_override=None,
    )
    return session


def test_skill_listing_is_reinjected_when_snapshot_is_bypassed() -> None:
    from linch.loop.request import _re_inject_skill_context

    session = _session()
    config = SimpleNamespace(_values={"profile": "overlay"}, context={})

    _re_inject_skill_context(session, config=config)

    assert any(
        "- alpha: Alpha skill" in getattr(block, "text", "")
        for message in session.provider_view
        for block in message.content
    )


def test_skill_reminder_is_provider_visible_but_not_historical() -> None:
    from linch.loop.request import _re_inject_skill_context

    session = _session()
    session.meta = {}
    session.agent.skill_listing_text = ""
    session.invoked_skills = (SimpleNamespace(name="alpha", substituted_body="Alpha body"),)

    _re_inject_skill_context(session, config=SimpleNamespace(_values=None, context={}))

    assert len(session.provider_view) == 1
    assert len(session.full_history) == 0
    assert isinstance(session.provider_view[0], Message)


def test_compaction_listing_fingerprint_uses_live_listing_for_snapshot_bypass() -> None:
    from linch.compaction import _skill_listing_fingerprint

    session = _session()
    config = SimpleNamespace(_values={"profile": "overlay"}, context={})

    expected = _skill_listing_fingerprint(session.agent.skill_listing_text)

    assert (
        _skill_listing_fingerprint(session.agent.skill_listing_text, session=session, config=config)
        == expected
    )
    assert _skill_listing_fingerprint(None, session=session, config=config) is None


@pytest.mark.asyncio
async def test_paid_recovery_passes_bypassed_config_to_skill_context(monkeypatch) -> None:
    import linch.compaction as compaction

    session = _session()
    agent = SimpleNamespace(compaction=None, compaction_ladder=None)
    ledger = SimpleNamespace(projections=lambda _kind: [], receipts=lambda: [])
    config = SimpleNamespace(_values={"profile": "overlay"}, context={})
    captured: dict[str, object] = {}

    def messages(_session, _agent, *, config=None):
        captured["config"] = config
        return []

    monkeypatch.setattr(compaction, "_skill_context_messages", messages)

    assert (
        await compaction._reconcile_paid_compactions(
            session, agent, ledger, turn_index=0, config=config
        )
        is False
    )
    assert captured["config"] is config


@pytest.mark.asyncio
async def test_paid_recovery_keeps_live_listing_on_snapshot_bypass() -> None:
    import linch.compaction as compaction

    session = _session()
    strategy = compaction.DetailedCompaction(keep_recent_turns=1)
    session.agent.compaction = strategy
    session.agent.compaction_ladder = None
    config = SimpleNamespace(_values={"profile": "overlay"}, context={})
    history = [
        Message(role="user", content=[]),
        Message(role="assistant", content=[TextBlock(text="old")]),
        Message(role="user", content=[]),
        Message(role="assistant", content=[TextBlock(text="new")]),
    ]
    session.session_log = SessionLog.seed(historical=history, visible=history)
    reminder = compaction._skill_context_messages(session, session.agent, config=config)[0]
    strategy_config = compaction._builtin_compaction_config(strategy)
    target = compaction._builtin_paid_view(history, "summary", strategy_config)
    session.session_log = SessionLog.seed(historical=history, visible=[*target, reminder])
    recipe = {
        "delivery_attempt_id": "delivery",
        "strategy_config": strategy_config,
        "source_context_fingerprint": compaction._context_fingerprint(history),
        "full_history_base_count": len(history),
        "full_history_base_fingerprint": compaction._context_fingerprint(history),
        "source_message_fingerprints": compaction._source_message_fingerprints(history),
        "reminder_fingerprints": compaction._source_message_fingerprints([reminder]),
    }
    receipt = SimpleNamespace(
        kind="compaction",
        disposition="accepted",
        strategy_id=strategy.id,
        turn_index=0,
        delivery_attempt_id="delivery",
        accepted_text="summary",
        source_context_fingerprint=compaction._context_fingerprint(history),
    )

    class Ledger:
        def projections(self, kind):
            return [recipe] if kind == "compaction_source" else []

        def receipts(self):
            return [receipt]

        async def record_projection(self, _record):
            return None

    await compaction._reconcile_paid_compactions(
        session, session.agent, Ledger(), turn_index=0, config=config
    )

    assert any(
        "- alpha: Alpha skill" in getattr(block, "text", "")
        for message in session.provider_view
        for block in message.content
    )

"""Live: approvals suspend and resume a run, permission rules, `AskUser`."""

from __future__ import annotations

import json
from typing import Any

import pytest
from live_core_support import (
    PROVIDERS,
    Deny,
    Live,
    ask,
    make_config,
    retry_once,
    text_of,
)

from linch_rust import (
    Allow,
    Answer,
    InputRequestEvent,
    PermissionRequestEvent,
    SuspendedEvent,
    tool,
)

pytestmark = pytest.mark.live


def make_save_tool(saved: list[str]) -> Any:
    @tool(scope="write")
    async def save_note(text: str) -> str:
        """Save a note."""
        saved.append(text)
        return "saved"

    return save_note


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_write_tool_suspends_the_run_until_it_is_approved(live: Live) -> None:
    saved: list[str] = []
    seen_requests: list[dict[str, Any]] = []

    def decide(request: dict[str, Any]) -> Allow:
        seen_requests.append(request)
        assert saved == [], "the tool ran before it was approved"
        return Allow()

    trace = await ask(
        live,
        "Call save_note with text 'remember the milk', then say saved.",
        tools=(make_save_tool(saved),),
        decide=decide,
    )
    assert saved == ["remember the milk"]
    assert seen_requests and seen_requests[0]["tool_name"] == "save_note"
    kinds = [e.type for e in trace.events]
    # The first stream ended in permission_request + suspended, before the tool started.
    assert (
        kinds.index("permission_request")
        < kinds.index("suspended")
        < kinds.index("tool_call_start")
    )
    [suspended] = trace.of(SuspendedEvent)
    assert suspended.pending_request_ids == [seen_requests[0]["tool_use_id"]]
    assert trace.result.subtype == "success"
    assert len(trace.of(PermissionRequestEvent)) == 1


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_denied_call_never_runs_and_the_model_reads_the_reason(live: Live) -> None:
    saved: list[str] = []
    trace = await ask(
        live,
        "Call save_note with text 'secret plan'. If it is refused, explain the reason in one "
        "sentence.",
        tools=(make_save_tool(saved),),
        decide=lambda _request: Deny("notes are frozen until Monday"),
    )
    assert saved == []
    [end] = trace.ends("save_note")
    assert end.is_error and "notes are frozen until Monday" in text_of(end)
    assert trace.result.subtype == "success"
    assert "monday" in (trace.result.final_text or "").lower()


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_permission_rules_allow_and_deny_by_tool_without_suspending(live: Live) -> None:
    ran: list[str] = []

    @tool(scope="write")
    async def wipe_database() -> str:
        """Wipe the database."""
        ran.append("wipe")
        return "wiped"

    @tool(scope="write")
    async def save_note(text: str) -> str:
        """Save a note."""
        ran.append(text)
        return "saved"

    config = make_config(
        live,
        live.spec(max_turns=6),
        permissions={
            "mode": "default",
            "project_root": "/",
            "layers": [
                {
                    "name": "app",
                    "rules": [
                        {"type": "tool", "tool": "wipe_database", "decision": "deny"},
                        {"type": "tool", "tool": "save_note", "decision": "allow"},
                    ],
                }
            ],
        },
    )
    trace = await ask(
        live,
        "Do two steps. Step 1: call wipe_database (a refusal is expected and fine). Step 2: "
        "whatever step 1 returned, call save_note with text 'after'. Then say finished.",
        tools=(wipe_database, save_note),
        config=config,
    )
    assert ran == ["after"]
    assert not trace.of(PermissionRequestEvent) and not trace.of(SuspendedEvent)
    [wipe] = trace.ends("wipe_database")
    assert wipe.is_error and "denied" in text_of(wipe).lower() and "app" in text_of(wipe)
    assert not trace.ends("save_note")[0].is_error


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_unless_role_lets_an_admin_through_and_denies_everyone_else(live: Live) -> None:
    ran: list[str] = []

    @tool(scope="exec")
    async def admin_op() -> str:
        """Run the admin operation."""
        ran.append("ran")
        return "admin op done"

    def config() -> Any:
        return make_config(
            live,
            live.spec(max_turns=4),
            permissions={
                "mode": "skip_dangerous",
                "project_root": "/",
                "layers": [
                    {
                        "name": "roles",
                        "rules": [
                            {
                                "type": "tool",
                                "tool": "admin_op",
                                "decision": "deny",
                                "unless_role": "admin",
                            }
                        ],
                    }
                ],
            },
        )

    prompt = "Call admin_op, then say what happened."
    admin = await ask(
        live,
        prompt,
        tools=(admin_op,),
        config=config(),
        principal={"tenant": "acme", "user": "ada", "roles": ["admin"]},
    )
    assert ran == ["ran"] and not admin.ends("admin_op")[0].is_error

    ran.clear()
    guest = await ask(
        live,
        prompt,
        tools=(admin_op,),
        config=config(),
        principal={"tenant": "acme", "user": "bob", "roles": ["guest"]},
    )
    assert ran == []
    [end] = guest.ends("admin_op")
    assert end.is_error and "denied" in text_of(end).lower()


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_ask_user_suspends_for_an_answer_and_the_model_uses_it(live: Live) -> None:
    asked: list[InputRequestEvent] = []

    def answer(event: InputRequestEvent) -> Answer:
        asked.append(event)
        return Answer({q["id"]: "Blue" for q in event.questions})

    trace = await ask(
        live,
        "Call AskUser with one question (id 'color', header 'Color', question 'Pick a color', "
        "options Red and Blue). Then state which color the user picked.",
        config=make_config(live, ask_user=True),
        answers=answer,
    )
    [request] = asked
    assert request.kind == "ask_user" and request.questions
    [suspended] = trace.of(SuspendedEvent)
    assert suspended.status == "awaiting_input"
    [end] = trace.ends("AskUser")
    assert not end.is_error and json.loads(text_of(end))["answers"]
    assert "blue" in (trace.result.final_text or "").lower()

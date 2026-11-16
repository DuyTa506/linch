"""Redaction and verifiers, end to end against the real server."""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest
from scripted import config, text_turn

from linch_rust import (
    PartialAssistantEvent,
    Redaction,
    RedactionRule,
    ResultEvent,
    Server,
    Verification,
    VerificationEvent,
    Verifier,
    VerifierCheck,
    callback,
)

SECRET = "sk-live0123456789"
CORE = Path(__file__).resolve().parents[2] / "crates/linch-core/src"
REDACTION: Redaction = {"rules": [{"pattern": "sk-[A-Za-z0-9]{8,}", "replacement": "[KEY]"}]}


def with_spec(turns: list[dict[str, Any]], **spec: Any) -> Any:
    agent = cast(dict[str, Any], config(turns))
    agent["spec"] = {**agent["spec"], **spec}
    return agent


def verifications(envelopes: list[Any]) -> list[tuple[str, str, int]]:
    checks = [e.event for e in envelopes if isinstance(e.event, VerificationEvent)]
    return [(c.verifier, c.action, c.attempt) for c in checks]


def struct_fields(path: Path, name: str) -> set[str]:
    source = path.read_text(encoding="utf-8")
    body = source.split(f"pub struct {name} {{", 1)[1].split("\n}", 1)[0]
    return set(re.findall(r"^\s*pub (\w+):", body, re.MULTILINE))


@pytest.mark.source
def test_the_typed_dicts_name_every_field_the_server_reads() -> None:
    redaction = CORE / "redaction.rs"
    verification = CORE / "verification.rs"
    for typed, path, name in [
        (Redaction, redaction, "RedactionSpec"),
        (RedactionRule, redaction, "RedactionRule"),
        (Verification, verification, "VerificationSpec"),
        (Verifier, verification, "Verifier"),
    ]:
        keys = typed.__required_keys__ | typed.__optional_keys__
        assert keys == struct_fields(path, name), name
    source = verification.read_text(encoding="utf-8")
    check = source.split("pub enum Check {", 1)[1].split("\n}", 1)[0]
    fields = set(re.findall(r"^\s*(\w+): ", check, re.MULTILINE))
    assert VerifierCheck.__optional_keys__ | {"type"} == fields | {"type"}


async def test_the_raw_text_is_never_streamed() -> None:
    async with Server() as server:
        turns = [text_turn(f"the key is {SECRET}")]
        agent = await server.open_agent(with_spec(turns, redaction=REDACTION))
        run = await agent.run("s1", "go")
        envelopes = [envelope async for envelope in run]
        result = await run.result()

    assert SECRET not in repr([asdict(e.event) for e in envelopes])
    assert not any(isinstance(e.event, PartialAssistantEvent) for e in envelopes)
    assert isinstance(result, ResultEvent)
    assert result.final_text == "the key is [KEY]"


async def test_a_failed_answer_goes_back_and_a_stop_callback_shares_the_budget() -> None:
    @callback("stop", "polite", 1)
    def polite(request: dict[str, Any]) -> dict[str, Any]:
        if "please" in request["text"]:
            return {"action": "allow"}
        return {"action": "retry", "feedback": "Say please."}

    verification: Verification = {
        "max_retries": 2,
        "verifiers": [
            {"name": "cites", "check": {"type": "matches", "pattern": r"\[\d+\]"}},
        ],
    }
    turns = [text_turn("no"), text_turn("see [1]"), text_turn("see [1], please")]
    async with Server() as server:
        cfg = with_spec(turns, verification=verification)
        agent = await server.open_agent(cfg, callbacks=[polite])
        run = await agent.run("s1", "go")
        envelopes = [envelope async for envelope in run]
        result = await run.result()

    assert verifications(envelopes) == [("cites", "retry", 1), ("polite", "retry", 2)]
    assert isinstance(result, ResultEvent)
    assert result.final_text == "see [1], please"


async def test_a_stop_verifier_fails_the_run() -> None:
    verification: Verification = {
        "verifiers": [
            {
                "name": "no_keys",
                "check": {"type": "forbids", "pattern": "sk-"},
                "on_fail": "stop",
            }
        ]
    }
    async with Server() as server:
        cfg = with_spec([text_turn(f"use {SECRET}")], verification=verification)
        agent = await server.open_agent(cfg)
        run = await agent.run("s1", "go")
        envelopes = [envelope async for envelope in run]
        result = await run.result()

    assert verifications(envelopes) == [("no_keys", "stop", 0)]
    assert isinstance(result, ResultEvent)
    assert result.subtype == "error"

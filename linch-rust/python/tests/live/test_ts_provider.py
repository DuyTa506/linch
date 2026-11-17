"""Live toolsec: provider request settings and error scrubbing against a local echoing gateway
(#159, #162), tenant limits on those settings, and the server's stderr logging.

Expectations are in `toolsec/expectations.md` sections 7 and 8.2.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest
from live_sched_support import pg_dsn
from ts_support import (
    CANARY,
    FakeModel,
    fake_config,
    finish,
    needs_server,
    plan_model,
    pytestmark,
    record,
    server_env,
    serving,
    tenant,
    workspace,
)

from linch_rust import NotAllowed, OpenError, Server

__all__ = ["pg_dsn", "pytestmark"]

AREA = "provider"
ACME = "tok-acme-prov-0001"


def rand(n: int) -> str:
    return secrets.token_hex(n)[:n]


def echo_gateway() -> FakeModel:
    """Answers 400 with the request headers it received in the body."""

    def handler(seen: Any) -> Any:
        return (
            "http",
            400,
            json.dumps({"error": {"message": "bad request " + json.dumps(seen.headers)}}).encode(),
        )

    return FakeModel(handler)


def event_text(envelopes: list[Any]) -> str:
    return "\n".join(repr(e.event) for e in envelopes)


@needs_server
async def test_header_values_in_error_text_are_scrubbed_by_name_and_length() -> None:
    model = await echo_gateway().start()
    values = {
        "x-gateway-token": rand(6),  # credential-named, 6 bytes
        "x-trace-id": rand(6),  # plain name, 6 bytes: documented as not scrubbed
        "x-long-trace": rand(14),  # plain name, 14 bytes: scrubbed
        "x-secret-key": rand(6),  # credential-named by `secret`/`key`, 6 bytes
    }
    try:
        async with serving(
            {"acme": tenant(ACME, provider_base_urls=[model.url], provider_request=True)},
            env=server_env(LINCH_LOG="debug"),
        ) as proc:
            async with proc.connect(ACME) as server:
                config = fake_config(model, namespace="acme")
                config["provider"] = {
                    **config["provider"],
                    "request": {"default_headers": dict(values)},
                }
                agent = await server.open_agent(config)
                run = await agent.run("s1", "go")
                envelopes = await finish(agent, run)
                durable = await agent.events(run.id)
            log = proc.log
    finally:
        await model.stop()
    text = event_text(envelopes) + "\n" + event_text(durable)
    seen = {name: value in text for name, value in values.items()}
    in_log = {name: value in log for name, value in values.items()}
    wire = [r.headers for r in model.requests]
    record(
        AREA,
        "7-scrub",
        values_in_run_text=seen,
        values_in_debug_log=in_log,
        gateway_got=sorted(wire[0]) if wire else None,
        error_excerpt=[ln for ln in text.splitlines() if "bad request" in ln][:1],
        log_bytes=len(log),
    )
    assert wire, "the gateway never saw a request"
    assert all(wire[0].get(name) == value for name, value in values.items()), "headers not sent"
    assert "bad request" in text, "positive control: the gateway's error body reaches the run"
    assert not seen["x-gateway-token"], "a 6-byte credential-named header value leaked"
    assert not seen["x-secret-key"]
    assert not seen["x-long-trace"], "an 8+ byte value must be scrubbed"
    # documented: a plain-named header under 8 bytes is NOT scrubbed (recorded either way)
    assert seen["x-trace-id"] in (True, False)
    for name, value in values.items():
        assert not in_log[name], f"{name} value appears in the LINCH_LOG=debug stderr"


@needs_server
async def test_the_provider_key_is_not_in_errors_or_debug_logs() -> None:
    model = await echo_gateway().start()
    key = "sk-" + CANARY
    try:
        async with serving(
            {"acme": tenant(ACME, provider_base_urls=[model.url.rsplit("/v1", 1)[0]])},
            env=server_env(LINCH_LOG="debug", DEEPSEEK_API_KEY=key),
        ) as proc:
            async with proc.connect(ACME) as server:
                config = fake_config(model, namespace="acme")
                config["provider"] = {"id": "deepseek", "base_url": model.url.rsplit("/v1", 1)[0]}
                config["spec"]["model"] = "deepseek-flash"
                agent = await server.open_agent(config)
                run = await agent.run("s1", "go")
                envelopes = await finish(agent, run)
            log = proc.log
    finally:
        await model.stop()
    text = event_text(envelopes)
    sent = [r.headers.get("authorization") for r in model.requests]
    record(
        AREA,
        "7-key",
        gateway_saw_bearer=bool(sent and sent[0] and sent[0].startswith("Bearer ")),
        key_in_run_text=key in text or CANARY in text,
        key_in_log=key in log or CANARY in log,
    )
    assert sent and sent[0] == f"Bearer {key}", "positive control: the key reached the gateway"
    assert key not in text and CANARY not in text
    assert key not in log and CANARY not in log


@needs_server
async def test_a_tenant_with_provider_request_cannot_set_credential_or_routing_headers() -> None:
    model = await plan_model([]).start()
    try:
        async with serving(
            {
                "acme": tenant(ACME, provider_base_urls=[model.url], provider_request=True),
                "plain": tenant("tok-plain-prov-0001", provider_base_urls=[model.url]),
            }
        ) as proc:
            results: dict[str, Any] = {}
            secret = rand(10)
            async with proc.connect(ACME) as server:
                for header in (
                    "authorization",
                    "Authorization",
                    "x-api-key",
                    "api-key",
                    "x-goog-api-key",
                    "cookie",
                    "proxy-authorization",
                    "proxy-connection",
                    "x-forwarded-for",
                    "forwarded",
                    "host",
                    "content-length",
                    "transfer-encoding",
                    "connection",
                    "upgrade",
                ):
                    config = fake_config(model, namespace="acme")
                    config["provider"] = {
                        **config["provider"],
                        "request": {"default_headers": {header: secret}},
                    }
                    try:
                        await server.open_agent(config)
                        results[header] = "OPENED"
                    except (NotAllowed, OpenError) as error:
                        kind = getattr(error, "kind", None)
                        results[header] = (type(error).__name__, kind, str(error)[:160])
                        assert secret not in str(error), f"{header}: the refusal echoes the value"
                # bounds
                bounds: dict[str, Any] = {}
                for label, request in (
                    ("17_headers", {"default_headers": {f"x-h{i}": "v" for i in range(17)}}),
                    ("long_value", {"default_headers": {"x-big": "v" * 1100}}),
                    ("body_17k", {"extra_body": {"blob": "v" * 17000}}),
                    ("timeout_600001", {"timeout_ms": 600001}),
                    ("ok_small", {"default_headers": {"x-ok": "v"}, "timeout_ms": 5000}),
                ):
                    config = fake_config(model, namespace="acme")
                    config["provider"] = {**config["provider"], "request": request}
                    try:
                        await server.open_agent(config)
                        bounds[label] = "OPENED"
                    except (NotAllowed, OpenError) as error:
                        bounds[label] = (type(error).__name__, getattr(error, "kind", None))
            async with proc.connect("tok-plain-prov-0001") as other:
                config = fake_config(model, namespace="plain")
                config["provider"] = {**config["provider"], "request": {"timeout_ms": 1000}}
                with pytest.raises(NotAllowed) as plain:
                    await other.open_agent(config)
                config = fake_config(model, namespace="plain")
                config["provider"] = {**config["provider"], "chat": {"parallel_tool_calls": False}}
                with pytest.raises(NotAllowed) as plain_chat:
                    await other.open_agent(config)
                results["no_allow_request"] = plain.value.kind
                results["no_allow_chat"] = plain_chat.value.kind
    finally:
        await model.stop()
    record(AREA, "7-header-admission", headers=results, bounds=bounds)
    for header in (
        "authorization",
        "Authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "cookie",
        "proxy-authorization",
        "proxy-connection",
        "x-forwarded-for",
        "forwarded",
        "host",
    ):
        assert results[header] != "OPENED", header
    assert results["no_allow_request"] == "provider_request"
    assert results["no_allow_chat"] == "provider_request"
    assert bounds["ok_small"] == "OPENED"
    for label in ("17_headers", "long_value", "body_17k", "timeout_600001"):
        assert bounds[label] != "OPENED", label


@needs_server
async def test_extra_body_cannot_replace_messages_tool_choice_or_model() -> None:
    wire: list[dict[str, Any]] = []

    def handler(seen: Any) -> Any:
        wire.append(seen.body)
        return ("text", "ok")

    model = await FakeModel(handler).start()
    try:
        with workspace() as root:
            async with Server(env=server_env()) as server:
                config = fake_config(
                    model,
                    workspace=str(root),
                    permissions={"mode": "skip_dangerous", "project_root": str(root)},
                )
                config["provider"] = {
                    **config["provider"],
                    "request": {
                        "extra_body": {
                            "tool_choice": "none",
                            "messages": [{"role": "user", "content": "HIJACKED"}],
                            "model": "hijack-model",
                            "tools": [],
                            "temperature": 0.123,
                            "top_k": 40,
                            "stream": False,
                        }
                    },
                }
                agent = await server.open_agent(config)
                await finish(agent, await agent.run("s1", "the-real-prompt"))
    finally:
        await model.stop()
    body = wire[0]
    record(
        AREA,
        "7-extra-body",
        keys=sorted(body),
        model=body.get("model"),
        tool_choice=body.get("tool_choice"),
        tools=len(body.get("tools", [])),
        temperature=body.get("temperature"),
        stream=body.get("stream"),
        messages_have_prompt="the-real-prompt" in json.dumps(body.get("messages")),
        messages_have_hijack="HIJACKED" in json.dumps(body.get("messages")),
    )
    assert body.get("top_k") == 40, "positive control: extra_body is applied"
    assert body.get("temperature") != 0.123, "temperature is protected"
    assert "the-real-prompt" in json.dumps(body["messages"]) and "HIJACKED" not in json.dumps(
        body["messages"]
    )
    assert body["model"] == "fake"
    assert body.get("tool_choice") != "none"
    assert len(body.get("tools", [])) > 0
    assert body.get("stream") is True


@needs_server
async def test_server_log_levels_and_a_provoked_warning(pg_dsn: str) -> None:
    """LINCH_LOG default is warn; RUST_LOG is read when LINCH_LOG is unset; a bad filter falls back."""
    import asyncio

    outcomes: dict[str, Any] = {}
    failing = FakeModel(lambda seen: ("http", 400, b"deliberately failing gateway"))
    await failing.start()
    try:
        for label, extra_env in (
            ("default", {}),
            ("LINCH_LOG=info", {"LINCH_LOG": "info"}),
            ("LINCH_LOG=debug", {"LINCH_LOG": "debug"}),
            ("RUST_LOG=info", {"RUST_LOG": "info"}),
            ("LINCH_LOG=bogus[", {"LINCH_LOG": "bogus["}),
        ):
            allow: dict[str, Any] = {
                "provider_base_urls": [failing.url],
                "postgres_dsns": [pg_dsn],
                "max_queued_jobs": 4,
            }
            # server A queues a job; server B's worker refuses it (its allowlist lost the
            # provider endpoint), which is a failed job: a `warn` record
            async with serving({"acme": tenant(ACME, **allow)}, env=server_env()) as first:
                async with first.connect(ACME) as client:
                    config = fake_config(
                        failing, namespace="acme", stores={"type": "postgres", "dsn": pg_dsn}
                    )
                    agent = await client.open_agent(config)
                    await agent.enqueue("s1", "go")
            tightened = {"postgres_dsns": [pg_dsn], "max_queued_jobs": 4}
            worker = {"dsn": {"value": pg_dsn}, "poll_interval_ms": 200, "retry_delay_ms": 300}
            async with serving(
                {"acme": tenant(ACME, **tightened)},
                env=server_env(**extra_env),
                extra={"worker": worker},
            ) as proc:
                await asyncio.sleep(3)
                err = proc.log
            levels = sorted(
                {lv for lv in ("ERROR", "WARN", "INFO", "DEBUG", "TRACE") if f" {lv} " in err}
            )
            outcomes[label] = {
                "stderr_bytes": len(err),
                "levels": levels,
                "head": err[:500],
                "dsn_password_in_log": "postgres:postgres" in err or pg_dsn in err,
            }
    finally:
        await failing.stop()
    record(AREA, "8.2-log-levels", **outcomes)
    for label in (
        "default",
        "LINCH_LOG=info",
        "LINCH_LOG=debug",
        "RUST_LOG=info",
        "LINCH_LOG=bogus[",
    ):
        assert not outcomes[label]["dsn_password_in_log"], f"{label}: DSN credentials in the log"
    assert outcomes["default"]["levels"], "default: the failed job shows no warning"
    assert set(outcomes["default"]["levels"]) <= {"WARN", "ERROR"}
    assert outcomes["LINCH_LOG=debug"]["stderr_bytes"] > outcomes["default"]["stderr_bytes"]
    assert "not a valid log filter" in outcomes["LINCH_LOG=bogus["]["head"]

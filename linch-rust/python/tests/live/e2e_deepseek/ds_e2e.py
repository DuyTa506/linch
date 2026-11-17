"""DeepSeek live e2e scenarios (see expectations.md).

    ds_e2e.py [--mock] <scenario> [<scenario> ...]      scenarios: s3 s1 s2 s5 s6 s7 s8

`--mock` answers every call from the gateway's canned replies (rehearsal, no network, key not needed).
Without it the gateway forwards to DeepSeek and the key comes from the environment (the wrapper script
sources it); the key is handed to the server process only and is never written anywhere.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import random
import shutil
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).parent))
from gateway import Gateway, message_hashes  # noqa: E402

from linch_rust import OpenError, RpcError, Server, tool  # noqa: E402

ROOT = Path("/home/aiqk3/Desktop/duy_dev/linch-agent-scratch/e2e/results/deepseek")
MODEL = "deepseek-flash"
PROBE = "e2e-probe-9f3a7c21"
PRICING = {MODEL: {"input": 0.3, "output": 1.2, "cache_read": 0.006}}
CODE = "MARMOT-7741"
MOCK = "--mock" in sys.argv
TAG = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--tag=")), "")


def log(*parts: Any) -> None:
    print(*parts, file=sys.stderr, flush=True)


# -- text helpers -----------------------------------------------------------------------------
WORDS = (
    "alpha basalt cobalt delta ember fjord garnet harbor indigo juniper kelp lantern meadow nickel "
    "orchid prairie quartz ridge saffron tundra umber velvet willow xenon yarrow zephyr anchor beacon "
    "canyon dune estuary forge glacier hollow island jetty kiln lagoon mesa nebula oasis pylon quarry "
    "reef summit thicket upland valley wharf yonder zenith"
).split()


def filler(chars: int, seed: str) -> str:
    rng = random.Random(seed)
    out: list[str] = []
    size = 0
    while size < chars:
        word = f"{rng.choice(WORDS)}-{rng.randint(10, 99)}"
        out.append(word)
        size += len(word) + 1
    return " ".join(out)[:chars].rstrip()


def exact(text: str, chars: int) -> str:
    return (text + " " + filler(chars, text))[:chars]


# -- environment ------------------------------------------------------------------------------
def server_env() -> dict[str, str]:
    key = "sk-mock-not-a-key" if MOCK else os.environ["DEEPSEEK_API_KEY"]
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"), "DEEPSEEK_API_KEY": key}


def pg_schema() -> tuple[str, str]:
    base = os.environ["LINCH_TEST_PG_DSN"]
    schema = f"ds_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}options=-c%20search_path%3D{schema}", schema


def drop_schema(schema: str) -> None:
    with psycopg.connect(os.environ["LINCH_TEST_PG_DSN"], autocommit=True) as admin:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def config(gw: Gateway, spec: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None, **top: Any) -> Any:
    spec_all: dict[str, Any] = {
        "model": MODEL,
        "max_output_tokens": 200,
        "thinking": {"type": "disabled"},
        "call_timeout_ms": 90_000,
        "max_turns": 2,
        "provider_retry": {"max_attempts": 1},
        "system_prompt": "You are a terse test assistant. Follow instructions exactly.",
    }
    spec_all.update(spec or {})
    return {
        "namespace": f"e2e-{uuid.uuid4().hex[:8]}",
        "spec": spec_all,
        "provider": {
            "id": "deepseek",
            "base_url": gw.url,
            "request": {"default_headers": headers if headers is not None else {"x-e2e-probe": PROBE}},
        },
        **top,
    }


def ev_dict(envelope: Any) -> dict[str, Any]:
    event = envelope.event
    return {"seq": envelope.seq, "ts_ms": envelope.ts_ms, "type": event.type, **dataclasses.asdict(event)}


async def run_collect(agent: Any, session: str, prompt: str, timeout: float = 150, **kw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async with asyncio.timeout(timeout):
        run = await agent.run(session, prompt, **kw)
        async for envelope in run:
            if "partial" in envelope.event.type:
                continue
            out.append(ev_dict(envelope))
    return out


def of(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == kind]


def result_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    results = of(events, "result")
    return results[-1] if results else {}


def save(name: str, data: Any) -> None:
    out = ROOT / ("rehearsal" if MOCK else "live")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}{TAG}.json").write_text(json.dumps(data, indent=1, default=str))


def new_records(gw: Gateway, before: int) -> list[dict[str, Any]]:
    return gw.records()[before:]


def load_bodies(gw: Gateway) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((gw.dir / "bodies").glob("*.json")):
        out[path.stem] = json.loads(path.read_text())
    return out


# -- scenarios --------------------------------------------------------------------------------
PERSON = {
    "name": "person",
    "description": "The person described in the text.",
    "schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name", "age"],
        "additionalProperties": False,
    },
}


def lookup_tool() -> Any:
    @tool(scope="read")
    async def lookup(email: str) -> str:
        """Find a customer by email."""
        return "Ada Lovelace"

    return lookup


@tool(scope="read")
async def clock() -> str:
    """Return the time."""
    return "12:00"


def common_prefix(a: list[Any], b: list[Any]) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else json.dumps(content)


async def s3(gw: Gateway) -> dict[str, Any]:
    """Compaction summary request vs the primary request's prefix."""
    nonce = uuid.uuid4().hex[:10]
    dsn, schema = pg_schema()
    mark = len(gw.records())
    try:
        spec = {
            "max_output_tokens": 120,
            "max_turns": 2,
            "system_prompt": f"Test assistant, session nonce {nonce}. Answer tersely and follow instructions exactly. Never use tools.",
            "compaction": {"threshold_tokens": 3250, "keep_recent_turns": 1, "max_output_tokens": 500},
        }
        cfg = config(gw, spec, pricing=PRICING, stores={"type": "postgres", "dsn": dsn})
        gw.control(max_tokens_cap=600)
        turns: list[dict[str, Any]] = []
        async with Server(env=server_env()) as server:
            agent = await server.open_agent(cfg)
            # Seed (mock replies, not forwarded): about 3000 estimated tokens (chars / 4).
            gw.control(mock=True, delay=0)
            seeds = [
                ("Hello, I am preparing a deployment.", exact("Understood, noted.", 3200)),
                (f"For your records: the project codename is {CODE}. " + filler(2800 - 60, "u2"), exact("Recorded.", 3200)),
                ("Log excerpt, no action needed: " + filler(2800 - 31, "u3"), "Noted."),
            ]
            for index, (user, reply) in enumerate(seeds, 1):
                gw.control(text=reply)
                events = await run_collect(agent, "long", user)
                turns.append({"turn": f"seed{index}", "result": result_of(events).get("subtype"), "compaction": of(events, "compaction")})
            gw.control(mock=MOCK, text="Summary of the conversation so far: the user prepares a deployment and gave the code word. " + "x" * 120)
            live: list[tuple[str, str, int]] = [
                ("A", "Reply with the single word: ready.", 0),
                ("B", "Log excerpt, no action needed: " + filler(1600 - 31 - 60, "uB") + f" Question: what is the project codename I gave you earlier? Reply with the codename only.", 4),
                ("C", "Reply with the single word: done.", 0),
            ]
            for label, prompt, pause in live:
                if pause and not MOCK:
                    await asyncio.sleep(pause)
                events = await run_collect(agent, "long", prompt)
                turns.append(
                    {
                        "turn": label,
                        "result": result_of(events),
                        "usage": of(events, "usage"),
                        "compaction": of(events, "compaction"),
                        "assistant_text": [e["message"] for e in of(events, "assistant")][-1:],
                        "load": os.getloadavg(),
                    }
                )
        records = new_records(gw, mark)
        bodies = load_bodies(gw)
        return {"scenario": "s3", "nonce": nonce, "turns": turns, "gateway": records, "analysis": analyse_compaction(gw, records, bodies)}
    finally:
        drop_schema(schema)


def analyse_compaction(gw: Gateway, records: list[dict[str, Any]], bodies: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Message-level view of the four live requests: A, summary, B primary, C."""
    live = [r for r in records if r["kind"] == ("mock" if MOCK else "forwarded") and r.get("status") == 200 and r["path"].endswith("completions")]
    # The seed requests are mock; keep the ones after the seed (arrival order, last four).
    live = live[-4:] if not MOCK else live[-4:]
    out: dict[str, Any] = {"requests": []}
    keyed: list[dict[str, Any]] = []
    for record in live:
        key = f"mock-{record.get('mock_seq', 0):03d}" if MOCK else f"{record['seq']:03d}"
        keyed.append({"record": record, "key": key})
    # Bodies are matched by order: forwarded bodies by seq; mock bodies by position among mocks.
    if MOCK:
        names = sorted(k for k in bodies if k.startswith("mock-"))[-len(live):]
    else:
        names = [f"{r['seq']:03d}" for r in live]
    msgs = [bodies[n]["messages"] for n in names]
    for label, name, record, messages in zip(["A", "summary", "B_primary", "C"], names, live, msgs, strict=False):
        usage = record.get("usage") or {}
        hit = usage.get("prompt_cache_hit_tokens")
        prompt = usage.get("prompt_tokens")
        out["requests"].append(
            {
                "label": label,
                "body": name,
                "messages": len(messages),
                "roles": [m["role"] for m in messages][-6:],
                "last_message_head": message_text(messages[-1])[:200],
                "usage": usage,
                "hit_ratio": (hit / prompt) if hit is not None and prompt else None,
                "max_tokens": record.get("max_tokens"),
                "t_start": record.get("t_start"),
                "t_end": record.get("t_end"),
                "tools": record.get("tools"),
                "tool_choice": record.get("tool_choice"),
                "verbatim_blocks": sum(message_text(m).count("[user message, verbatim]") for m in messages),
                "summary_marker": any("<summary of earlier conversation>" in message_text(m) for m in messages),
            }
        )
    if len(msgs) >= 2:
        a, s = msgs[0], msgs[1]
        out["A_messages"] = len(a)
        out["summary_messages"] = len(s)
        out["common_prefix_messages"] = common_prefix(a, s)
        out["summary_extra_after_A"] = len(s) - len(a)
        out["summary_trailing_role"] = s[-1]["role"]
        out["summary_trailing_head"] = message_text(s[-1])[:400]
        out["summary_prefix_identical_to_A"] = common_prefix(a, s) == len(a)
        out["summary_gap_s"] = (live[1].get("t_start") or 0) - (live[0].get("t_end") or 0)
        out["B_primary_vs_summary_common"] = common_prefix(msgs[1], msgs[2]) if len(msgs) > 2 else None
    return out


async def s1(gw: Gateway) -> dict[str, Any]:
    """Final-tool structured output, tool_choice required / named, forced choice with thinking."""
    mark = len(gw.records())
    gw.control(mock=MOCK, max_tokens_cap=300, delay=0, args={"person": {"name": "Ada Lovelace", "age": 36}, "lookup": {"email": "ada@example.com"}})
    out: dict[str, Any] = {"scenario": "s1", "load": os.getloadavg()}
    async with Server(env=server_env()) as server:
        # 1a/1b: the final tool.
        cfg = config(gw, {"output_schema": PERSON, "structured_output_retries": 0, "max_turns": 3})
        agent = await server.open_agent(cfg)
        events = await run_collect(agent, "s", "Extract the person: Ada Lovelace was 36 years old when she died.")
        init = of(events, "init")[0]
        calls = [b for a in of(events, "assistant") for b in a["message"]["content"] if b.get("type") == "tool_use"]
        out["final_tool"] = {
            "init_final_tool": init.get("final_tool"),
            "result": result_of(events),
            "usage_events": len(of(events, "usage")),
            "assistant_calls_person": sum(1 for c in calls if c["name"] == "person"),
            "tool_call_start_person": sum(1 for e in of(events, "tool_call_start") if e["tool_name"] == "person"),
        }
        # 1c: required and named.
        for label, choice in (("required", {"type": "required"}), ("named", {"type": "tool", "name": "lookup"})):
            cfg = config(gw, {"tool_choice": choice, "max_turns": 1})
            agent = await server.open_agent(cfg, tools=[lookup_tool(), clock])
            events = await run_collect(agent, "s", "Say hello to the user.")
            first = of(events, "assistant")[0]["message"]["content"] if of(events, "assistant") else []
            out[label] = {
                "tool_uses": [b["name"] for b in first if b.get("type") == "tool_use"],
                "result": result_of(events),
                "error": of(events, "error"),
                "loop_guard": of(events, "loop_guard"),
            }
        # 1d: forced choice with thinking on: capabilities, then open.
        think_cfg = config(gw, {"thinking": {"type": "enabled"}, "tool_choice": {"type": "required"}, "max_turns": 1})
        caps = await server.provider_capabilities(think_cfg)
        out["capabilities"] = dataclasses.asdict(caps) if dataclasses.is_dataclass(caps) else str(caps)
        try:
            await server.open_agent(think_cfg, tools=[lookup_tool(), clock])
            out["thinking_forced_open"] = "opened"
        except RpcError as error:
            out["thinking_forced_open"] = {"type": type(error).__name__, "code": getattr(error, "code", None), "message": str(error)}
    # Raw API probe: what DeepSeek itself says to a forced choice with thinking on (one request).
    if "--probe" in sys.argv:
        body = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50,
            "stream": False,
            "thinking": {"type": "enabled"},
            "tools": [{"type": "function", "function": {"name": "lookup", "description": "x", "parameters": {"type": "object", "properties": {}, "required": []}}}],
            "tool_choice": "required",
        }
        key = server_env()["DEEPSEEK_API_KEY"]
        req = urllib.request.Request(gw.url + "/chat/completions", data=json.dumps(body).encode(), headers={"authorization": f"Bearer {key}", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                out["probe"] = {"status": response.status, "body": response.read().decode()[:600]}
        except urllib.error.HTTPError as error:
            out["probe"] = {"status": error.code, "body": error.read().decode()[:600]}
    out["gateway"] = new_records(gw, mark)
    return out


async def s2(gw: Gateway) -> dict[str, Any]:
    """Prompt cache on two requests with a long fixed prefix; cost arithmetic."""
    nonce = uuid.uuid4().hex[:10]
    mark = len(gw.records())
    gw.control(mock=MOCK, max_tokens_cap=300, delay=0, text="blue")
    system = f"Session nonce {nonce}. Reference text, ignore its content: " + filler(8000, "ref") + " Answer every question with one short word."
    cfg = config(gw, {"max_output_tokens": 20, "max_turns": 1, "system_prompt": system, "budget": {"max_cost_usd": 0.05}}, pricing=PRICING)
    turns: list[dict[str, Any]] = []
    async with Server(env=server_env()) as server:
        agent = await server.open_agent(cfg)
        for index, line in enumerate(["Question 1: name a colour.", "Question 2: name an animal."]):
            events = await run_collect(agent, f"c{index}", line)
            turns.append({"events": of(events, "usage"), "result": result_of(events), "load": os.getloadavg()})
            if index == 0 and not MOCK:
                await asyncio.sleep(6)
    records = new_records(gw, mark)
    return {"scenario": "s2", "nonce": nonce, "turns": turns, "gateway": records, "pricing": PRICING}


async def s5(gw: Gateway) -> dict[str, Any]:
    mark = len(gw.records())
    gw.control(mock=MOCK, max_tokens_cap=300, delay=0, args={"lookup": {"email": "ada@example.com"}})
    out: dict[str, Any] = {"scenario": "s5"}
    async with Server(env=server_env()) as server:
        try:
            await server.open_agent(config(gw, {"budget": {"max_cost_usd": 1.0}}))
            out["no_pricing_open"] = "opened"
        except RpcError as error:
            out["no_pricing_open"] = {"type": type(error).__name__, "code": getattr(error, "code", None), "message": str(error)}
        cfg = config(gw, {"tool_choice": {"type": "required"}, "max_turns": 4, "budget": {"max_cost_usd": 1e-6}}, pricing=PRICING)
        agent = await server.open_agent(cfg, tools=[lookup_tool()])
        events = await run_collect(agent, "cap", "Look up ada@example.com.")
        out["tiny_cap"] = {"events": [e for e in events if e["type"] in ("budget", "error", "result", "usage", "tool_call_end")]}
    out["gateway"] = new_records(gw, mark)
    return out


async def s6(gw: Gateway) -> dict[str, Any]:
    mark = len(gw.records())
    gw.control(mock=MOCK, max_tokens_cap=300, delay=0, text="391")
    cfg = config(gw, {"thinking": {"type": "enabled"}, "effort": "high", "max_output_tokens": 300, "max_turns": 1}, pricing=PRICING)
    async with Server(env=server_env()) as server:
        agent = await server.open_agent(cfg)
        events = await run_collect(agent, "t", "What is 17 times 23? Reply with just the number.")
    return {"scenario": "s6", "usage": of(events, "usage"), "result": result_of(events), "errors": of(events, "error"), "types": sorted({e["type"] for e in events}), "gateway": new_records(gw, mark), "load": os.getloadavg()}


async def s7(gw: Gateway) -> dict[str, Any]:
    out: dict[str, Any] = {"scenario": "s7", "gates": {}}
    for gate in (1, 3):
        mark = len(gw.records())
        gw.control(mock=MOCK, max_tokens_cap=300, delay=0.7 if MOCK else 0, text="1, 2, 3")
        cfg = config(gw, {"max_output_tokens": 80, "max_turns": 1}, max_provider_concurrency=gate)
        async with Server(env=server_env()) as server:  # a fresh server per gate: agents share one limit
            agent = await server.open_agent(cfg)
            t0 = time.time()
            events = await asyncio.gather(*[run_collect(agent, f"g{gate}-{i}", "Count from 1 to 20 separated by commas, nothing else.") for i in range(3)])
            wall = time.time() - t0
        records = [r for r in new_records(gw, mark) if r["kind"] in ("forwarded", "mock")]
        intervals = sorted((r["t_start"], r["t_end"]) for r in records)
        points = sorted([(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals], key=lambda p: (p[0], p[1]))
        peak = cur = 0
        for _, delta in points:
            cur += delta
            peak = max(peak, cur)
        result_ts = [[(e["ts_ms"], e["type"]) for e in run if e["type"] in ("init", "usage", "result")] for run in events]
        out["gates"][gate] = {
            "wall_s": wall,
            "peak_in_flight_at_gateway": peak,
            "intervals": intervals,
            "subtypes": [result_of(run).get("subtype") for run in events],
            "event_ts": result_ts,
            "gateway": records,
            "load": os.getloadavg(),
        }
    return out


async def s8(gw: Gateway) -> dict[str, Any]:
    mark = len(gw.records())
    gw.control(mock=MOCK, max_tokens_cap=300)
    cfg = config(gw, {"max_turns": 1}, headers={"x-e2e-probe": PROBE, "x-e2e-mode": "echo400"})
    async with Server(env=server_env()) as server:
        agent = await server.open_agent(cfg)
        events = await run_collect(agent, "h", "hello")
    dump = json.dumps(events)
    return {
        "scenario": "s8",
        "probe_value": PROBE,
        "value_in_events": PROBE in dump,
        "redacted_in_events": "[redacted]" in dump,
        "errors": of(events, "error"),
        "result": result_of(events),
        "gateway": new_records(gw, mark),
    }


SCENARIOS = {"s3": s3, "s1": s1, "s2": s2, "s5": s5, "s6": s6, "s7": s7, "s8": s8}


async def main() -> None:
    names = [a for a in sys.argv[1:] if not a.startswith("--")]
    out_dir = ROOT / ("rehearsal" if MOCK else "live")
    if MOCK and out_dir.exists():
        shutil.rmtree(out_dir)
    gw = Gateway(out_dir).start()
    log(f"gateway {gw.url} mock={MOCK} forwarded_so_far={gw.forwarded()} load={os.getloadavg()}")
    try:
        for name in names:
            if not MOCK and gw.forwarded() >= 30:
                log("limit reached")
                break
            before = gw.forwarded()
            result = await SCENARIOS[name](gw)
            save(name, result)
            log(f"{name} done: forwarded {gw.forwarded() - before} (total {gw.forwarded()})")
    finally:
        gw.stop()


if __name__ == "__main__":
    asyncio.run(main())

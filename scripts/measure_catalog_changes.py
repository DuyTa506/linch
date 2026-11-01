"""Measure what mid-session catalog changes do to a provider's prompt cache.

Every scenario sends a seed request, repeats it so the prefix is warm, then sends
one later turn carrying the change. Only the last request's cached tokens matter.
Payloads are plain JSON so this measures the wire, not Linch's adapters.

    python scripts/measure_catalog_changes.py --provider anthropic --model MODEL
    python scripts/measure_catalog_changes.py --provider responses --model MODEL
    python scripts/measure_catalog_changes.py --provider chat --model MODEL \
        --base-url http://127.0.0.1:8000/v1
    python scripts/measure_catalog_changes.py --provider chat --dry-run --model m

Credentials come from ANTHROPIC_API_KEY or OPENAI_API_KEY. A local server needs none.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from typing import Any

ANTHROPIC_BETA = "inline-tools-2026-09-15"
SCENARIOS: dict[str, tuple[str, ...]] = {
    "anthropic": (
        "control",
        "tail_text",
        "native_add",
        "native_edit",
        "native_remove",
        "prefix_append",
    ),
    "responses": ("control", "tail_text", "native_add", "native_remove", "prefix_append"),
    "chat": ("control", "tail_text", "prefix_append", "prefix_middle", "prefix_edit"),
}
NOTICE = "<system-reminder>Catalog update: skill 'deploy' is now available.</system-reminder>"


def _tool(name: str, field: str = "query") -> dict[str, Any]:
    return {
        "name": name,
        "description": f"Run the {name} operation against the workspace.",
        "schema": {
            "type": "object",
            "properties": {field: {"type": "string", "description": f"Input for {name}."}},
            "required": [field],
        },
    }


def base_conversation(nonce: str, padding: int) -> dict[str, Any]:
    system = f"Session {nonce}. Reply in one short sentence.\n" + " ".join(
        f"Stable reference rule {index} keeps this prefix long." for index in range(padding)
    )
    return {
        "system": system,
        "tools": [_tool(f"tool_{index:02d}") for index in range(12)],
        "messages": [
            {"role": "user", "content": "Name one prime number."},
            {"role": "assistant", "content": "Seven is prime."},
            {"role": "user", "content": "Name another prime number."},
        ],
    }


def _anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": tool["name"],
        "description": tool["description"],
        "input_schema": tool["schema"],
    }


def _anthropic_payload(
    conversation: dict[str, Any], model: str, tail: list[dict[str, Any]]
) -> dict[str, Any]:
    messages = deepcopy(conversation["messages"])
    last = messages[-1]
    last["content"] = [
        {"type": "text", "text": last["content"], "cache_control": {"type": "ephemeral"}}
    ]
    return {
        "model": model,
        "max_tokens": 32,
        "system": [
            {
                "type": "text",
                "text": conversation["system"],
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "tools": [_anthropic_tool(tool) for tool in conversation["tools"]],
        "messages": messages + tail,
    }


def _responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool["name"],
        "description": tool["description"],
        "parameters": tool["schema"],
    }


def _responses_payload(
    conversation: dict[str, Any], model: str, tail: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "model": model,
        "max_output_tokens": 32,
        "instructions": conversation["system"],
        "tools": [_responses_tool(tool) for tool in conversation["tools"]],
        "input": deepcopy(conversation["messages"]) + tail,
    }


def _chat_payload(
    conversation: dict[str, Any], model: str, tail: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 32,
        "messages": [
            {"role": "system", "content": conversation["system"]},
            *deepcopy(conversation["messages"]),
            *tail,
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["schema"],
                },
            }
            for tool in conversation["tools"]
        ],
    }


BUILDERS = {
    "anthropic": _anthropic_payload,
    "responses": _responses_payload,
    "chat": _chat_payload,
}


def _prefix_change(conversation: dict[str, Any], scenario: str) -> None:
    tools = conversation["tools"]
    if scenario == "prefix_append":
        tools.append(_tool("tool_new"))
    elif scenario == "prefix_middle":
        tools.insert(len(tools) // 2, _tool("tool_new"))
    elif scenario == "prefix_edit":
        tools[len(tools) // 2] = _tool(tools[len(tools) // 2]["name"], "changed")


def _tail(provider: str, scenario: str) -> list[dict[str, Any]]:
    if scenario == "tail_text":
        role = "system" if provider == "anthropic" else "user"
        return [{"role": role, "content": NOTICE}]
    if provider == "anthropic" and scenario == "native_add":
        definition = _anthropic_tool(_tool("tool_new"))
        content = [
            {
                "type": "tool_addition",
                "tool": {"type": "tool_definition", "definition": definition},
            }
        ]
    elif provider == "anthropic" and scenario == "native_edit":
        definition = _anthropic_tool(_tool("tool_06", "changed"))
        content = [
            {
                "type": "tool_addition",
                "tool": {"type": "tool_definition", "definition": definition},
            }
        ]
    elif provider == "anthropic" and scenario == "native_remove":
        content = [{"type": "tool_removal", "tool": {"type": "tool_reference", "name": "tool_06"}}]
    elif provider == "responses" and scenario == "native_add":
        return [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [_responses_tool(_tool("tool_new"))],
            }
        ]
    else:
        return []
    return [{"role": "system", "content": content}]


def build_requests(
    provider: str, scenario: str, model: str, nonce: str, padding: int
) -> list[dict[str, Any]]:
    """Return the seed, warm, and measured payloads of one scenario.

    Args:
        provider: Wire family, one of ``SCENARIOS``.
        scenario: Change applied on the measured request.
        model: Provider model identifier.
        nonce: Value isolating this scenario from earlier cache entries.
        padding: Number of stable system sentences.

    Returns:
        Three payloads in send order.
    """
    build = BUILDERS[provider]
    conversation = base_conversation(nonce, padding)
    seed = build(conversation, model, [])
    later = deepcopy(conversation)
    later["messages"] += [
        {"role": "assistant", "content": "Eleven is prime."},
        {"role": "user", "content": "Name a third prime number."},
    ]
    _prefix_change(later, scenario)
    measured = build(later, model, _tail(provider, scenario))
    if provider == "responses" and scenario == "native_remove":
        names = [tool["name"] for tool in later["tools"] if tool["name"] != "tool_06"]
        measured["tool_choice"] = {
            "type": "allowed_tools",
            "mode": "auto",
            "tools": [{"type": "function", "name": name} for name in names],
        }
    return [seed, deepcopy(seed), measured]


def cached_tokens(provider: str, body: dict[str, Any]) -> tuple[int, int]:
    """Return ``(cached, total_input)`` from a provider response body.

    Args:
        provider: Wire family that produced *body*.
        body: Decoded JSON response.

    Returns:
        Tokens read from cache and total input tokens.
    """
    usage = body.get("usage") or {}
    if provider == "anthropic":
        cached = int(usage.get("cache_read_input_tokens") or 0)
        written = int(usage.get("cache_creation_input_tokens") or 0)
        return cached, cached + written + int(usage.get("input_tokens") or 0)
    if provider == "responses":
        details = usage.get("input_tokens_details") or {}
        return int(details.get("cached_tokens") or 0), int(usage.get("input_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    timings = body.get("timings") or {}
    cached = int(timings.get("cache_n") or details.get("cached_tokens") or 0)
    if timings:
        return cached, cached + int(timings.get("prompt_n") or 0)
    return cached, int(usage.get("prompt_tokens") or 0)


def _endpoint(args: argparse.Namespace, payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
    headers = {"content-type": "application/json"}
    if args.provider == "anthropic":
        base = args.base_url or "https://api.anthropic.com/v1"
        headers |= {
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        }
        rendered = json.dumps(payload["messages"])
        if "tool_addition" in rendered or "tool_removal" in rendered:
            headers["anthropic-beta"] = ANTHROPIC_BETA
        return f"{base}/messages", headers
    if key := os.environ.get("OPENAI_API_KEY"):
        headers["authorization"] = f"Bearer {key}"
    base = args.base_url or "https://api.openai.com/v1"
    return f"{base}/{'responses' if args.provider == 'responses' else 'chat/completions'}", headers


def _send(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    url, headers = _endpoint(args, payload)
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=args.timeout) as response:
        return json.load(response)


def run_scenario(args: argparse.Namespace, scenario: str) -> dict[str, Any]:
    """Send one scenario and report the measured request's cache share.

    Args:
        args: Parsed command-line options.
        scenario: Scenario name.

    Returns:
        JSON row with cached and total tokens, or the provider's error.
    """
    nonce = uuid.uuid4().hex
    payloads = build_requests(args.provider, scenario, args.model, nonce, args.padding)
    if args.dry_run:
        return {"scenario": scenario, "payloads": payloads}
    try:
        for payload in payloads[:-1]:
            _send(args, payload)
            time.sleep(args.delay)
        cached, total = cached_tokens(args.provider, _send(args, payloads[-1]))
    except urllib.error.HTTPError as exc:
        return {"scenario": scenario, "error": exc.code, "detail": exc.read().decode()[:500]}
    share = round(cached / total, 3) if total else 0.0
    return {"scenario": scenario, "cached": cached, "total": total, "share": share}


def main() -> int:
    """Run the selected scenarios and print one JSON object per line.

    Returns:
        Process exit code: non-zero when a scenario failed.
    """
    parser = argparse.ArgumentParser(description="Measure prompt-cache effects of catalog changes.")
    parser.add_argument("--provider", choices=sorted(SCENARIOS), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--scenario", action="append", help="repeatable; default: all")
    parser.add_argument("--padding", type=int, default=400)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    failed = False
    for scenario in args.scenario or SCENARIOS[args.provider]:
        if scenario not in SCENARIOS[args.provider]:
            parser.error(f"unknown scenario for {args.provider}: {scenario}")
        row = run_scenario(args, scenario)
        failed = failed or "error" in row
        print(json.dumps(row), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

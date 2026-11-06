"""Regenerate the committed bench fixtures in `fixtures/`.

    python make_fixtures.py            write every fixture
    python make_fixtures.py --check    fail if a committed fixture differs

The loop fixtures use the W5 scripted format (one `{"events": [...]}` per
provider turn); `loop_10x1.json` and `loop_10x3.json` are the W5 files and
this script reproduces them byte for byte. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODEL = "scripted-model"
USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_creation_tokens": 0}


def text_turn(text: str) -> dict:
    return {
        "events": [
            {"type": "message_start", "model": MODEL},
            {"type": "text_delta", "text": text},
            {"type": "message_end", "stop_reason": "end_turn", "usage": USAGE},
        ]
    }


def tool_turn(calls: list[tuple[str, str, str]], text: str = "working") -> dict:
    """One provider turn calling each `(id, name, input_json)` in order."""
    events: list[dict] = [
        {"type": "message_start", "model": MODEL},
        {"type": "text_delta", "text": text},
    ]
    for call, name, raw in calls:
        events += [
            {"type": "tool_use_start", "id": call, "name": name},
            {"type": "tool_use_input_delta", "id": call, "json_delta": raw},
            {"type": "tool_use_end", "id": call},
        ]
    events.append({"type": "message_end", "stop_reason": "tool_use", "usage": USAGE})
    return {"events": events}


def loop(turns: int, tools: int) -> dict:
    """N tool turns of k Echo calls, then a text turn (the W5 shape)."""
    usage = USAGE
    out = []
    for t in range(turns):
        events = [
            {"type": "message_start", "model": MODEL},
            {"type": "text_delta", "text": "working"},
        ]
        for k in range(tools):
            call = f"call_{t}_{k}"
            events += [
                {"type": "tool_use_start", "id": call, "name": "Echo"},
                {"type": "tool_use_input_delta", "id": call, "json_delta": '{"value": '},
                {"type": "tool_use_input_delta", "id": call, "json_delta": f'"v{t}.{k}"}}'},
                {"type": "tool_use_end", "id": call},
            ]
        events.append({"type": "message_end", "stop_reason": "tool_use", "usage": usage})
        out.append({"events": events})
    out.append(text_turn("done"))
    return {"turns": out}


def all_fixtures() -> dict[str, dict]:
    return {
        "loop_10x1.json": loop(10, 1),
        "loop_10x3.json": loop(10, 3),
        "loop_50x1.json": loop(50, 1),
        "loop_1x8.json": loop(1, 8),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for name, data in all_fixtures().items():
        text = json.dumps(data)
        path = FIXTURES / name
        if args.check:
            if not path.is_file() or path.read_text() != text:
                stale.append(name)
        else:
            path.write_text(text)
    if stale:
        sys.exit(f"stale fixtures: {', '.join(stale)}")


if __name__ == "__main__":
    main()

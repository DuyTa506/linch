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


def calls(name: str, inputs: list[dict]) -> dict:
    """One tool turn per input, each calling `name` once, then a text turn."""
    turns = [tool_turn([(f"call_{i}", name, json.dumps(raw))]) for i, raw in enumerate(inputs)]
    return {"turns": [*turns, text_turn("done")]}


# Compaction: 15 Echo turns of a 4000-character value. Rust estimates 2006
# tokens per turn (text, tool input and result at 4 characters a token), so
# at a 25 000-token threshold it compacts once, before call 13; the summary
# call takes the turn placed there. Keep in step with COMPACTION in run_all.
COMPACTION_TURNS = 15
COMPACTION_AT = 13
SUMMARY = "SUMMARY: the earlier turns echoed long values."


def compaction() -> dict:
    turns = []
    for t in range(COMPACTION_TURNS):
        if t == COMPACTION_AT:
            turns.append(text_turn(SUMMARY))
        value = f"c{t:02d}" + "x" * 3997
        turns.append(tool_turn([(f"call_{t}", "Echo", json.dumps({"value": value}))]))
    return {"turns": [*turns, text_turn("done")]}


def tools() -> dict[str, dict]:
    """The workspace tool fixtures; paths are relative to the workspace root
    that `run_all.py` builds (`src/file_00.txt` .. `file_09.txt`, `tree/`)."""
    files = [f"src/file_{i:02d}.txt" for i in range(10)]
    edit = []
    for i, path in enumerate(files):
        edit.append(("Read", {"file_path": path}))
        edit.append(
            ("Edit", {"file_path": path, "old_string": f"marker_{i}", "new_string": "done"})
        )
    edit_fixture = {
        "turns": [
            *(
                tool_turn([(f"call_{i}", name, json.dumps(raw))])
                for i, (name, raw) in enumerate(edit)
            ),
            text_turn("done"),
        ]
    }
    # Python's default loop guard stops a run after three identical calls,
    # so each call's input differs while the work stays the same: an
    # alternative that never matches, an equivalent glob, another echo.
    grep = [
        {"pattern": f"needle|absent_{i}", "path": "src", "output_mode": "files_with_matches"}
        for i in range(10)
    ]
    grep2k = [
        {"pattern": f"needle_hit|absent_{i}", "path": "tree", "output_mode": "files_with_matches"}
        for i in range(5)
    ]
    globs = [
        "**/*.txt",
        "**/*.[t]xt",
        "**/*.t[x]t",
        "**/*.tx[t]",
        "**/*.[t][x]t",
        "**/*.[t]x[t]",
        "**/*.t[x][t]",
        "**/*.[t][x][t]",
        "**/*[.]txt",
        "**/*[.][t]xt",
    ]
    return {
        "tools_read.json": calls("Read", [{"file_path": path} for path in files]),
        "tools_write.json": calls(
            "Write",
            [
                {"file_path": f"src/new_{i:02d}.txt", "content": f"new file {i}\n" * 20}
                for i in range(10)
            ],
        ),
        "tools_edit.json": edit_fixture,
        "tools_grep.json": calls("Grep", grep),
        # Glob is the one schema that differs: Rust `pattern`/`path`, Python
        # `glob_pattern`/`target_directory`. Same search either way.
        "tools_glob.json": calls("Glob", [{"pattern": g, "path": "src"} for g in globs]),
        "tools_glob_py.json": calls(
            "Glob", [{"glob_pattern": g, "target_directory": "src"} for g in globs]
        ),
        "tools_bash.json": calls("Bash", [{"command": f"echo bench {i}"} for i in range(10)]),
        "tools_grep2k.json": calls("Grep", grep2k),
    }


def evals(n: int = 10) -> dict:
    """N eval cases, each one Echo call then the expected answer."""
    cases, turns = [], []
    for i in range(n):
        cases.append({"prompt": f"What is item {i}?", "expected": f"answer {i}"})
        turns.append(tool_turn([(f"call_{i}", "Echo", json.dumps({"value": f"item {i}"}))]))
        turns.append(text_turn(f"The answer {i}."))
    return {"cases": cases, "turns": turns}


def all_fixtures() -> dict[str, dict]:
    return {
        "loop_10x1.json": loop(10, 1),
        "loop_10x3.json": loop(10, 3),
        "loop_50x1.json": loop(50, 1),
        "loop_1x8.json": loop(1, 8),
        "compaction.json": compaction(),
        **tools(),
        "mcp_echo.json": calls("mcp__bench__echo", [{"text": f"ping {i}"} for i in range(10)]),
        "evals.json": evals(),
        "text.json": {"turns": [text_turn("done")]},
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

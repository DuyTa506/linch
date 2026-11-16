"""Long-term memory in a folder of Markdown notes, like an Obsidian vault.

The agent reads its memory through `retrieval`, a `context_build` callback:
before every model call it searches the notes with the user's latest
question and adds the best ones to that request only. It writes memory
through `remember`, a host tool that saves a note. Swap `NoteStore` for a
Notion database, a vector index or a SQL table: anything with `retrieve`.

The model here is scripted, so the example runs offline:

    python examples/external_memory.py
"""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from linch_rust import AgentConfig, Callback, Envelope, Server, ToolCallEndEvent, tool
from linch_rust.memory import Chunk, retrieval
from linch_rust.scripted import MODEL, text_turn, tool_turn

_WORD = re.compile(r"[a-z0-9]{3,}")


class NoteStore:
    """Markdown notes in one folder, searched by shared words."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def retrieve(self, query: str, *, k: int) -> list[Chunk]:
        """The `k` notes sharing the most words with `query`."""
        words = set(_WORD.findall(query.lower()))
        scored = []
        for path in sorted(self.root.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            score = len(words & set(_WORD.findall(f"{path.stem} {text}".lower())))
            if score:
                scored.append(Chunk(id=path.name, source=str(path), text=text, score=score))
        return sorted(scored, key=lambda chunk: -chunk.score)[:k]

    def remember(self, title: str, text: str) -> str:
        """Save `text` as the note `title`, replacing an older one."""
        name = re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-") or "note"
        (self.root / f"{name}.md").write_text(text, encoding="utf-8")
        return f"saved {name}.md"


def remember_tool(store: NoteStore) -> Any:
    """The host tool the model writes its memory with."""

    @tool(scope="write")
    def remember(title: str, text: str) -> str:
        """Save a fact for later conversations, as a short titled note."""
        return store.remember(title, text)

    return remember


def config() -> AgentConfig:
    """Two scripted runs: one saves a note, the next is answered from it."""
    turns = [
        tool_turn(
            "call_1", "remember", {"title": "deploy day", "text": "Deploys happen on Tuesdays."}
        ),
        text_turn("Noted, and the team is called Linch."),
        text_turn("On Tuesdays."),
    ]
    return {
        "namespace": "memory-demo",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "permissions": {"mode": "accept_edits", "project_root": "/"},
    }


async def main(vault: Path, extra: Sequence[Callback] = ()) -> list[list[Envelope]]:
    """Run both conversations against the notes in `vault`; their envelopes."""
    store = NoteStore(vault)
    store.remember("team", "The team is called Linch.")
    callbacks = [retrieval(store, k=3), *extra]
    runs = []
    async with Server() as server:
        agent = await server.open_agent(config(), tools=[remember_tool(store)], callbacks=callbacks)
        for prompt in [
            "Remember that deploys happen on Tuesdays. What is our team called?",
            "When do deploys happen?",
        ]:
            run = await agent.run("s1", prompt)
            runs.append([envelope async for envelope in run])
    return runs


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        for envelopes in asyncio.run(main(Path(root))):
            for envelope in envelopes:
                if isinstance(envelope.event, ToolCallEndEvent):
                    print("tool:", envelope.event.result)
            print("answer:", envelopes[-1].event)
    sys.exit(0)

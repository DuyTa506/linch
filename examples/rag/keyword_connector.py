"""Dependency-free reference connector; production hosts supply their own ACLs/search."""

from __future__ import annotations

import asyncio
from typing import Any

from linch import RetrievedChunk


class KeywordConnector:
    resume_policy_id = "example.keyword-corpus"
    resume_policy_version = 1

    def __init__(self, chunks: list[RetrievedChunk], *, corpus_version: str) -> None:
        self.chunks = chunks
        self.resume_policy_config = {"corpus_version": corpus_version}

    async def retrieve(self, query: str, *, k: int, principal: Any = None) -> list[RetrievedChunk]:
        words = set(query.lower().split())
        ranked = [
            (len(words.intersection(chunk.text.lower().split())), chunk) for chunk in self.chunks
        ]
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [chunk for score, chunk in ranked[:k] if score]


async def main() -> None:
    connector = KeywordConnector(
        [
            RetrievedChunk(
                "refunds", "policy/refunds", "Refunds are available for 30 days.", 1.0, {}
            )
        ],
        corpus_version="2026-10",
    )
    print(await connector.retrieve("refunds", k=5))


if __name__ == "__main__":
    asyncio.run(main())

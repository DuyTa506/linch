from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_vector_memory_docs_point_to_optional_recipes():
    docs = (ROOT / "docs" / "usage" / "vector-memory-adapters.md").read_text(encoding="utf-8")

    assert "MemoryStore" in docs
    assert "faiss_adapter.py" in docs
    assert "pgvector_memory.py" in docs
    assert "qdrant_adapter.py" in docs
    assert "Vector DB dependencies stay in your application" in docs


def test_vector_adapter_recipes_use_memory_store_shape_without_core_changes():
    for relative in [
        "examples/memory/faiss_adapter.py",
        "examples/memory/pgvector_memory.py",
        "examples/memory/qdrant_adapter.py",
    ]:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "MemoryItem" in text
        assert "MemorySearchResult" in text
        assert "async def search(" in text
        assert "async def upsert(" in text

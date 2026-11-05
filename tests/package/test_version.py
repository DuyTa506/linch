"""``get_version`` is read on every run; the metadata lookup must happen once."""

from __future__ import annotations

from importlib import metadata

import pytest

import linch._version as version


def test_get_version_reads_package_metadata_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_version(name: str) -> str:
        calls.append(name)
        return "9.9.9"

    monkeypatch.setattr(metadata, "version", fake_version)
    version.get_version.cache_clear()
    try:
        assert version.get_version() == "9.9.9"
        assert version.get_version() == "9.9.9"
    finally:
        version.get_version.cache_clear()
    assert calls == ["linch"]

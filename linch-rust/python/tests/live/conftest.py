"""Live end-to-end tests: real models, real processes. See `live_core_support`."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live: drives a real model through the server; skips without "
        "LINCH_LIVE_VLLM_URL / DEEPSEEK_API_KEY",
    )

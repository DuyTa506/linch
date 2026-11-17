"""Live end-to-end tests: real models, real processes. See `README.md`."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the live suites unless the run asks for them with `-m live`.

    A plain `pytest` (the unit gate) never starts a model-backed or process-heavy scenario, even
    when a Postgres DSN or a server binary happens to be set.
    """
    if "live" in (config.getoption("-m") or ""):
        return
    reason = "live suite: run with `pytest -m live` (see tests/live/README.md)"
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "tests/live/" in item.nodeid.replace("\\", "/"):
            item.add_marker(skip)

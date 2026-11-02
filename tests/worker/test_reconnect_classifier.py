"""A disconnect that races an in-flight query must rebuild the manager."""

from __future__ import annotations

import pytest

pytest.importorskip("pgqueuer")
asyncpg_exceptions = pytest.importorskip("asyncpg.exceptions")

from linch.worker.runtime import _is_worker_reconnect_error  # noqa: E402

InternalClientError = asyncpg_exceptions.InternalClientError


def test_asyncpg_state_race_during_disconnect_rebuilds_manager() -> None:
    error = InternalClientError("cannot switch to state 12; another operation (2) is in progress")

    assert _is_worker_reconnect_error(error)


def test_other_internal_client_errors_stay_fatal() -> None:
    assert not _is_worker_reconnect_error(InternalClientError("unexpected codec state"))

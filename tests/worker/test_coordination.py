"""Completion wakes release fleet capacity without idle unlimited-worker probes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("queues", [{"default": 0}])
async def test_driver_filters_only_valid_unlimited_completion_hints_and_removes_listener(queues):
    pytest.importorskip("pgqueuer")
    from pgqueuer import QueueManager

    from linch.worker import Worker

    assert callable(getattr(QueueManager, "_effective_dequeue_timeout", None))
    assert callable(getattr(QueueManager, "fetch_jobs", None))
    callbacks = set()

    class Connection:
        async def add_listener(self, channel, callback):
            callbacks.add((channel, callback))

        async def remove_listener(self, channel, callback):
            callbacks.remove((channel, callback))

    worker = Worker(SimpleNamespace(get_max_size=lambda: 2), lambda job: None, queues=queues)
    manager = worker._make_manager(Connection())
    # A timeout probe would fail: this fake connection has no SQL methods.
    assert await manager._effective_dequeue_timeout(timedelta(seconds=7)) == timedelta(seconds=7)
    driver = manager.queries.driver
    received = []
    await driver.add_listener("channel", received.append)
    await driver.add_listener("channel", received.append)
    assert len(callbacks) == 1
    ((_, relay),) = callbacks
    event = dict(
        type="table_changed_event",
        channel="channel",
        table="pgqueuer",
        sent_at=datetime.now(UTC).isoformat(),
    )
    envelopes = [
        json.dumps(dict(event, operation=op)) for op in ("insert", "update", "delete", "truncate")
    ]
    envelopes += [
        '{"type":"table_changed_event","operation":"delete"}',
        '{"type":"unknown"}',
        "not json",
        '{"type":"health_check_event","id":"x"}',
        '{"type":"cancellation_event","ids":[1]}',
    ]
    for payload in envelopes:
        relay(None, None, "channel", payload)
    expected = envelopes if any(queues.values()) else envelopes[:2] + envelopes[4:]
    assert received == expected
    await driver.remove_listener("channel", received.append)
    assert not callbacks

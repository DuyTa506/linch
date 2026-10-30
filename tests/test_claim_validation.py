"""Delivery claims retain the same validation order across coordination stores."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.parametrize("kind", ["mailbox", "schedule"])
@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        ({"owner": "", "lease_s": -1, "now": float("nan"), "limit": -1}, "owner must be non-empty"),
        ({"lease_s": True}, "lease_s must be a positive finite number"),
        (
            {"lease_s": float("inf"), "now": float("nan")},
            "lease_s must be a positive finite number",
        ),
        ({"now": True}, "now must be finite"),
        ({"now": float("nan"), "limit": -1}, "now must be finite"),
        ({"limit": True}, "limit must be a non-negative integer or None"),
        ({"limit": -1}, "limit must be a non-negative integer or None"),
    ],
)
async def test_claim_validation_error_order(
    kind: str, invalid: dict[str, Any], message: str
) -> None:
    from linch.coordination.mailbox.core import InMemoryMailbox
    from linch.coordination.scheduling.store import InMemoryScheduleStore

    args = {"owner": "worker", "lease_s": 30, "now": 100, "limit": None, **invalid}
    with pytest.raises(ValueError) as error:
        if kind == "mailbox":
            await InMemoryMailbox().claim("recipient", **args)
        else:
            await InMemoryScheduleStore().claim_due_occurrences(**args)
    assert str(error.value) == message

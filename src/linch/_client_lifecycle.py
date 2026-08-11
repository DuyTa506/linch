from __future__ import annotations

import asyncio
import inspect
from typing import Any


def running_loop() -> Any:
    """The running event loop, or ``None`` outside one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def loop_changed(bound_loop: Any) -> bool:
    """Whether a client built on *bound_loop* is now being used from another loop.

    Provider SDKs bind their transport to the loop alive at construction, so a
    cached client is only reusable on that loop. A host that runs one loop per
    task (Celery, scripts, some test harnesses) otherwise hits
    ``Event loop is closed`` on the second use.
    """
    if bound_loop is None:
        return False
    current = running_loop()
    return current is not None and current is not bound_loop


async def aclose_client(client: Any) -> None:
    """Close a duck-typed client, preferring its asynchronous close method."""
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    result = closer()
    if inspect.isawaitable(result):
        await result

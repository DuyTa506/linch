from __future__ import annotations

import inspect
from typing import Any


async def aclose_client(client: Any) -> None:
    """Close a duck-typed client, preferring its asynchronous close method."""
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    result = closer()
    if inspect.isawaitable(result):
        await result

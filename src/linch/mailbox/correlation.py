"""Neutral request/response correlation FSM.

A primitive, *not* a protocol: it tracks which outstanding ``request_id`` values
are still pending and resolves one when a matching response (``in_reply_to``)
arrives. It is intentionally non-blocking — a turn-based agent cannot block its
turn awaiting a peer, so it opens a request, continues, and polls
:meth:`is_resolved` / :meth:`response` on a later turn after draining its inbox.
What counts as a "request" or "response" is entirely the embedder's choreography.
"""

from __future__ import annotations

from .core import MailboxMessage


class Correlator:
    """Pending → resolved state machine keyed by ``request_id``."""

    def __init__(self) -> None:
        # dict (not set) so pending() preserves registration order.
        self._pending: dict[str, None] = {}
        self._responses: dict[str, MailboxMessage] = {}

    def open(self, request_id: str) -> None:
        """Register an outstanding request awaiting a response."""
        if request_id not in self._responses:
            self._pending.setdefault(request_id, None)

    def resolve(self, response: MailboxMessage) -> bool:
        """Match *response* to its open request via ``in_reply_to``.

        Returns ``True`` only on the first match for a request; a response with
        no ``in_reply_to``, for an unknown/closed request, or a duplicate
        returns ``False`` (first response wins).
        """
        request_id = response.in_reply_to
        if request_id is None or request_id not in self._pending:
            return False
        del self._pending[request_id]
        self._responses[request_id] = response
        return True

    def is_resolved(self, request_id: str) -> bool:
        return request_id in self._responses

    def response(self, request_id: str) -> MailboxMessage | None:
        return self._responses.get(request_id)

    def pending(self) -> list[str]:
        """Request ids still awaiting a response, in registration order."""
        return list(self._pending)

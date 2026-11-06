"""`Agent`: an opened agent handle, and the approval decisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ._rpc import Connection
from .events import Envelope, decode_envelope
from .run import Run


@dataclass(frozen=True)
class Allow:
    """Approve a pending tool call."""


@dataclass(frozen=True)
class Deny:
    """Refuse a pending tool call; the model reads `reason`."""

    reason: str


Decision = Allow | Deny


class Agent:
    """An agent the server opened; made by `Server.open_agent`.

    With memory stores, its runs live in this handle: resume and approve them
    through the same `Agent`. With Postgres stores, any handle of the same
    config (on any server) can.

    Attributes:
        handle: The server's handle for the agent, such as `agent-1`.
        contract: Digest of the spec, tools and callback policies; a run is
            resumed or approved only under the contract it started with.
    """

    def __init__(self, conn: Connection, handle: str, contract: str) -> None:
        self.handle = handle
        self.contract = contract
        self._conn = conn

    async def run(self, session_id: str, prompt: str) -> Run:
        """Start a run of `prompt` in a session, which is created when missing.

        Args:
            session_id: The conversation the prompt continues.
            prompt: The user's text.

        Returns:
            The started run; iterate it or await `Run.result()`.
        """
        params = {"agent": self.handle, "session_id": session_id, "prompt": prompt}
        return await self._start("run.start", params)

    async def resume(self, run_id: str) -> Run:
        """Continue a run after a crash or a dropped stream.

        A run waiting for approval suspends again.
        """
        return await self._start("run.resume", {"agent": self.handle, "run_id": run_id})

    async def approve(self, run_id: str, decisions: Mapping[str, Decision]) -> Run:
        """Answer a suspended run's pending calls and continue it.

        Args:
            run_id: The suspended run.
            decisions: An `Allow()` or `Deny(reason)` per `tool_use_id` from
                the `suspended` event's `pending_request_ids`; a pending call
                with no decision is asked again.

        Returns:
            The continued run.
        """
        wire = {call: _decision(decision) for call, decision in decisions.items()}
        params = {"agent": self.handle, "run_id": run_id, "decisions": wire}
        return await self._start("run.approve", params)

    async def events(self, run_id: str, after_seq: int = 0) -> list[Envelope]:
        """Return a run's durable envelopes with a seq above `after_seq`, in order.

        Stream-only kinds (`partial_assistant`, `tool_progress`, `hook`) are
        never replayed.

        Raises:
            RunError: The stores could not be read, or the run is unknown.
        """
        params = {"agent": self.handle, "run_id": run_id, "after_seq": after_seq}
        answer = await self._conn.request("run.events", params)
        return [decode_envelope(raw) for raw in answer["events"]]

    async def _start(self, method: str, params: dict[str, Any]) -> Run:
        run_id, queue = await self._conn.request_run(method, params)
        return Run(self._conn, run_id, queue)


def _decision(decision: Decision) -> dict[str, str]:
    if isinstance(decision, Deny):
        return {"decision": "deny", "reason": decision.reason}
    return {"decision": "allow"}

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

    async def run(self, session_id: str, prompt: str, *, run_id: str | None = None) -> Run:
        """Start a run of `prompt` in a session, which is created when missing.

        A session has at most one run that is not terminal; starting another
        ends it with `SessionBusy`, naming the live run.

        Args:
            session_id: The conversation the prompt continues.
            prompt: The user's text.
            run_id: The new run's id, so it can be resumed even if this call
                never returns; the server picks one when absent. An id
                already stored ends the run with a `RunError` (-32023).

        Returns:
            The started run; iterate it or await `Run.result()`.

        Raises:
            RunBusy: This server already drives a run with `run_id`.
        """
        params: dict[str, Any] = {"agent": self.handle, "session_id": session_id, "prompt": prompt}
        if run_id is not None:
            params["run_id"] = run_id
        return await self._start("run.start", params)

    async def resume(self, run_id: str) -> Run:
        """Continue a run after a crash or a dropped stream.

        A run waiting for approval suspends again.
        """
        return await self._start("run.resume", {"agent": self.handle, "run_id": run_id})

    async def approve(self, run_id: str, decisions: Mapping[str, Decision]) -> Run:
        """Answer a suspended run's pending calls and continue it.

        Only a run waiting for an approval takes it. Sent again after its
        answers were committed (after a crash or a dropped connection), the
        returned run ends with `NotSuspended` and nothing is written;
        continue that run with `resume`.

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

    async def close_session(self, session_id: str) -> None:
        """Tell the agent this client is done with a session.

        Its workspace tools forget what the session read, so a later write
        there must read the file again. Nothing is written: the session and
        its runs stay.

        Raises:
            AgentNotFound: The agent handle is unknown to the server.
        """
        await self._conn.request("session.close", {"agent": self.handle, "session_id": session_id})

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

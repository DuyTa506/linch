"""`Agent`: an opened agent handle, and the approval decisions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ._rpc import Connection, RunEnd, RunItem
from .config import Principal
from .errors import LinchError
from .events import Envelope, decode_envelope
from .run import Observer, Run


@dataclass(frozen=True)
class Allow:
    """Approve a pending tool call."""


@dataclass(frozen=True)
class Deny:
    """Refuse a pending tool call; the model reads `reason`."""

    reason: str


Decision = Allow | Deny


@dataclass(frozen=True)
class Skipped:
    """A skill directory or subagent file the agent left out when it opened.

    Attributes:
        path: The skill directory or subagent file.
        reason: Why: `missing_skill_md`, `missing_frontmatter`,
            `invalid_frontmatter`, `io_error`, `name_collision_tool`,
            `name_collision_skill` or `name_collision`.
        detail: What exactly is wrong, for a person to read.
    """

    path: str
    reason: str
    detail: str


class Agent:
    """An agent the server opened; made by `Server.open_agent`.

    With memory stores, its runs live in this handle: resume and approve them
    through the same `Agent`. With Postgres stores, any handle of the same
    config (on any server) can.

    Attributes:
        handle: The server's handle for the agent, such as `agent-1`.
        contract: Digest of the spec, tools and callback policies; a run is
            resumed or approved only under the contract it started with.
        skipped: The skill directories and subagent files of the config
            directory that were left out, and why; empty when none was.
    """

    def __init__(
        self,
        conn: Connection,
        handle: str,
        contract: str,
        observer: Callable[[], Observer | None] | None = None,
        skipped: Sequence[Skipped] = (),
    ) -> None:
        self.handle = handle
        self.contract = contract
        self.skipped = list(skipped)
        self._conn = conn
        # Makes the tracer of each run, resume or approve; None when untraced.
        self._observer = observer
        # One per call cancelled before its answer: cancels the run it started.
        self._abandoned: set[asyncio.Task[None]] = set()

    async def run(
        self,
        session_id: str,
        prompt: str,
        *,
        run_id: str | None = None,
        principal: Principal | None = None,
        trace_id: str | None = None,
    ) -> Run:
        """Start a run of `prompt` in a session, which is created when missing.

        A session has at most one run that is not terminal; starting another
        ends it with `SessionBusy`, naming the live run.

        Args:
            session_id: The conversation the prompt continues.
            prompt: The user's text.
            run_id: The new run's id, so it can be resumed even if this call
                never returns; the server picks one when absent. An id
                already stored ends the run with a `RunError` (-32023).
            principal: Who the run acts for, matched by permission rules
                with `role` or `unless_role`; none when absent.
            trace_id: Your trace id for the run, such as the current span's;
                stored with the run and echoed on every `InitEvent` and
                `ResultEvent` of it, a resume or approve included, and on its
                subagents' runs. None when absent.

        Returns:
            The started run; iterate it or await `Run.result()`.

        Raises:
            RunBusy: This server already drives a run with `run_id`.
        """
        params: dict[str, Any] = {"agent": self.handle, "session_id": session_id, "prompt": prompt}
        if run_id is not None:
            params["run_id"] = run_id
        if trace_id is not None:
            params["trace_id"] = trace_id
        return await self._start("run.start", params, principal)

    async def resume(self, run_id: str, *, principal: Principal | None = None) -> Run:
        """Continue a run after a crash or a dropped stream.

        A run waiting for approval suspends again. `principal` is who the
        rest of the run acts for, as in `run`.
        """
        params: dict[str, Any] = {"agent": self.handle, "run_id": run_id}
        return await self._start("run.resume", params, principal)

    async def approve(
        self,
        run_id: str,
        decisions: Mapping[str, Decision],
        *,
        principal: Principal | None = None,
    ) -> Run:
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
            principal: Who the rest of the run acts for, as in `run`.

        Returns:
            The continued run.
        """
        wire = {call: _decision(decision) for call, decision in decisions.items()}
        params: dict[str, Any] = {"agent": self.handle, "run_id": run_id, "decisions": wire}
        return await self._start("run.approve", params, principal)

    async def close(self) -> None:
        """Free the agent on the server: its stores and its MCP servers.

        It first waits for the runs started by a `run`, `resume` or
        `approve` call that was cancelled before its answer: each such run
        is cancelled as soon as the server names it. The server disconnects
        the MCP servers and waits for them first. With memory stores the
        agent's runs and sessions are gone; with Postgres stores they stay,
        for another agent of the same config.

        Raises:
            AgentNotFound: The agent is already closed.
            RunBusy: A run of the agent is still going on this connection;
                cancel it or read it to its end first.
        """
        await asyncio.gather(*self._abandoned)
        await self._conn.request("agent.close", {"agent": self.handle})
        self._conn.unbind(self.handle)

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

    async def _start(self, method: str, params: dict[str, Any], principal: Principal | None) -> Run:
        if principal is not None:
            params["principal"] = dict(principal)
        observer = self._observer() if self._observer is not None else None
        # Shielded: the server may start the run whatever happens to this
        # caller, so a cancelled one leaves the answer to `_abandon`.
        call = asyncio.ensure_future(self._conn.request_run(method, params))
        try:
            run_id, queue = await asyncio.shield(call)
        except asyncio.CancelledError:
            abandoned = asyncio.create_task(self._abandon(call))
            self._abandoned.add(abandoned)
            abandoned.add_done_callback(self._abandoned.discard)
            raise
        return Run(self._conn, run_id, queue, observer)

    async def _abandon(self, call: asyncio.Future[tuple[str, asyncio.Queue[RunItem]]]) -> None:
        # Cancel the run a cancelled call started, and read it to its end,
        # after which `close` may free the agent.
        try:
            run_id, queue = await call
            await self._conn.request("run.cancel", {"run_id": run_id})
        except LinchError:
            return  # Refused, so nothing started; or the connection is gone.
        while not isinstance(await queue.get(), RunEnd):
            pass


def _decision(decision: Decision) -> dict[str, str]:
    if isinstance(decision, Deny):
        return {"decision": "deny", "reason": decision.reason}
    return {"decision": "allow"}

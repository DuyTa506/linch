"""`Agent`: an opened agent handle, the approval decisions and the answers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import schedule as _schedule
from ._rpc import Connection, RunQueue
from .config import OutputSchema, Principal, ToolChoice
from .errors import LinchError
from .events import Envelope, decode_envelope
from .monitor import (
    CustomScorer,
    EvalCase,
    EvalReport,
    RunReportResult,
    Scorer,
    evaluate,
    run_report,
)
from .run import Observer, Run
from .schedule import CHECK_IN, Job, Schedule


@dataclass(frozen=True)
class Allow:
    """Approve a pending tool call."""


@dataclass(frozen=True)
class Deny:
    """Refuse a pending tool call; the model reads `reason`."""

    reason: str


Decision = Allow | Deny


@dataclass(frozen=True)
class Answer:
    """The user's answer to one `input_request` of an `AskUser` call.

    Attributes:
        answers: The answer per question id: one choice (normally an
            option's label; the user may write their own), or a list of
            choices for a `multi_select` question. Every question needs
            one unless the answer declines.
        note: A free note from the user, passed on to the model.
        accepted: `False` declines: the model is told to go on with stated
            assumptions. Use `Answer.declined`.
    """

    answers: Mapping[str, str | Sequence[str]] = field(
        default_factory=dict[str, "str | Sequence[str]"]
    )
    note: str = ""
    accepted: bool = True

    @classmethod
    def declined(cls, note: str = "") -> Answer:
        """An answer that declines the questions, with an optional note."""
        return cls(note=note, accepted=False)


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
        output_schema: OutputSchema | None = None,
        tool_choice: ToolChoice | None = None,
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
            output_schema: A JSON Schema this run's final answer must
                satisfy, in place of the agent's `spec.output_schema`; the
                agent's `structured_output_retries` still apply. The run
                stores it, so a resume or approve keeps it. The checked
                answer is `ResultEvent.structured_output`. None keeps the
                agent's.
            tool_choice: Whether and which tool the model must call in this
                run, in place of the agent's `spec.tool_choice` (see
                `ToolChoice`). The run stores it, so a resume or approve
                keeps it. None keeps the agent's.

        Returns:
            The started run; iterate it or await `Run.result()`. The call
            returns once the run is stored, so its id exists after any crash
            that follows (a refused start returns too, and the refusal ends
            the stream).

        Raises:
            RunBusy: This server already drives a run with `run_id`.
            InvalidOutputSchema: Raised by the run, not this call: the
                `output_schema` does not compile or its root is not an
                object, so no run was created.
            InvalidToolChoice: Raised by the run, not this call: the
                `tool_choice` names a tool the run does not offer, or the
                provider refuses it, so no run was created.
        """
        params: dict[str, Any] = {"agent": self.handle, "session_id": session_id, "prompt": prompt}
        if run_id is not None:
            params["run_id"] = run_id
        if trace_id is not None:
            params["trace_id"] = trace_id
        if output_schema is not None:
            params["output_schema"] = dict(output_schema)
        if tool_choice is not None:
            params["tool_choice"] = dict(tool_choice)
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

    async def answer(
        self,
        run_id: str,
        answers: Mapping[str, Answer],
        *,
        principal: Principal | None = None,
    ) -> Run:
        """Answer a run's pending `AskUser` questions and continue it.

        Only a run waiting for input (a `suspended` event with status
        `awaiting_input`) takes it. An answer that does not fit its
        questions ends the returned run with `InvalidAnswer` and nothing is
        written: the run still waits. Sent again after its answers were
        committed, it ends with `NotSuspended`; continue that run with
        `resume`.

        Args:
            run_id: The suspended run.
            answers: An `Answer` per `request_id` from the `input_request`
                events (the `suspended` event's `pending_request_ids`); a
                pending request with no answer is asked again.
            principal: Who the rest of the run acts for, as in `run`.

        Returns:
            The continued run.
        """
        wire = {request: _answer(answer) for request, answer in answers.items()}
        params: dict[str, Any] = {"agent": self.handle, "run_id": run_id, "answers": wire}
        return await self._start("run.answer", params, principal)

    async def steer(
        self,
        run_id: str,
        text: str,
        *,
        steer_id: str | None = None,
        principal: Principal | None = None,
    ) -> str:
        """Send text to a run that is going on; it reads it at its next turn boundary.

        The text is a user message of its own, logged after the tool results
        of the batch the run is in and before its next model call. When the
        model answers without calling a tool, it is logged before the run
        would end, and the run takes another turn. A `user` event with
        `subtype` `"alignment"` and the returned id in `steer_id` reports the
        delivery. A run waiting for an approval or an answer takes the steer
        too and delivers it once it continues; a run that ended refuses.

        The steer is in the run's journal when this returns: with Postgres
        stores a crash does not lose it, and a resumed run does not read it
        twice. With memory stores it is lost if the server stops. The run
        writes it at its next step, so the answer waits for the model call or
        tool batch in progress. The `user_prompt_submit` callbacks (the
        request has `source` `"steer"`) and prompt redaction see it as they
        see a prompt; a callback that blocks it drops it.

        Args:
            run_id: A run of this agent that is not finished.
            text: Not blank, at most 16 KiB.
            steer_id: Names the steer, so a call sent again after a dropped
                answer is taken once; letters, digits and `-_.:`, at most
                128 characters. A fresh id when absent.
            principal: Who steers, checked against a tenant's principal
                rules as for `run`.

        Returns:
            The steer's id.

        Raises:
            InvalidSteer: The text or id is not usable.
            RunNotFound: No such run in the agent's stores.
            RunTerminal: The run has finished.
            RunLeased: Another server drives the run, or a crashed holder's
                lease has not run out yet.
            LimitReached: The run holds 16 undelivered steers (`kind` is
                `steers`).
            RunError: The run stopped before it took the steer (code
                -32023); resume it and send the steer again with the same id.
        """
        params: dict[str, Any] = {"agent": self.handle, "run_id": run_id, "text": text}
        if steer_id is not None:
            params["id"] = steer_id
        if principal is not None:
            params["principal"] = dict(principal)
        return str((await self._conn.request("run.steer", params))["id"])

    async def cancel(self, run_id: str) -> bool:
        """Abort a run of this agent, live or suspended.

        A run this connection drives is aborted where it stands, as
        `Run.cancel` does. A run that waits for an approval or an answer,
        started here, by another process or before a restart (Postgres
        stores), is ended through the agent's stores: each pending call gets
        an aborted result, so the session stays usable, and the run ends
        with a `result` of subtype `aborted`, readable with `events`. A
        later `approve` or `answer` of it ends with `RunTerminal`.

        Args:
            run_id: The run to cancel.

        Returns:
            Whether the run was cancelled; `False` when there was nothing to
            cancel: the run is finished, or it is not suspended and this
            connection does not drive it.

        Raises:
            RunNotFound: No such run in the agent's stores.
            RunLeased: Another server drives the run.
            LeaseLost: An approve or answer took the run over while it was
                being cancelled; it goes on under that call.
            AgentNotFound: The agent is closed.
        """
        params = {"agent": self.handle, "run_id": run_id}
        return bool((await self._conn.request("run.cancel", params))["cancelled"])

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
        there must read the file again, and its background `Bash` commands
        are killed. Nothing is written: the session and its runs stay.

        Raises:
            AgentNotFound: The agent handle is unknown to the server.
        """
        await self._conn.request("session.close", {"agent": self.handle, "session_id": session_id})

    async def events(self, run_id: str, after_seq: int = 0) -> list[Envelope]:
        """Return a run's durable envelopes with a seq above `after_seq`, in order.

        Stream-only kinds (`partial_assistant`, `tool_progress`, `hook`,
        `cache_diagnostic`) are never replayed. The server answers a long run
        in pages; this reads them all, one call per page.

        Raises:
            RunError: The stores could not be read, or the run is unknown.
        """
        envelopes: list[Envelope] = []
        while True:
            params = {"agent": self.handle, "run_id": run_id, "after_seq": after_seq}
            answer = await self._conn.request("run.events", params)
            envelopes.extend(decode_envelope(raw) for raw in answer["events"])
            next_after_seq = answer.get("next_after_seq")
            if next_after_seq is None:
                return envelopes
            after_seq = next_after_seq

    async def eval(
        self,
        cases: Sequence[EvalCase],
        scorers: Sequence[Scorer | CustomScorer] = (),
        *,
        include_events: bool = False,
    ) -> EvalReport:
        """Run `cases` on this agent, one after another, and score each run.

        Each case runs on a fresh session and is stored like any run, so its
        `run_id` reads with `report`. A case passes when its run records no
        error and no scorer fails. The call returns when every case has run.

        Args:
            cases: The cases, in order; each prompt is not empty.
            scorers: Built-in scorers (see `Scorer`), which run in the
                server, and Python callables (see `CustomScorer`), which
                run here on each case's result once the cases are done. With
                none, a case passes when its run records no error.
            include_events: Whether each case carries every event of its run.

        Returns:
            The report: pass counts, cost, tokens and duration in total and
            per case, and the Markdown form.

        Raises:
            ProtocolError: A case or built-in scorer is invalid.
            AgentNotFound: The agent is closed.
            LimitReached: A remote tenant has no place for another run.
            TypeError: A custom scorer returned something but a bool or None.
        """
        return await evaluate(self._conn, self.handle, cases, scorers, include_events)

    async def report(self, run_id: str, *, timeline: bool = False) -> RunReportResult:
        """Return the report of a stored run, as a dict and as Markdown.

        It covers tool calls with their timings, token usage, cost, context
        builds, recovery and risk figures.

        Args:
            run_id: A run of this agent, finished or not.
            timeline: Whether the report carries every event.

        Raises:
            RunNotFound: The run is unknown.
            RunError: The stores could not be read.
        """
        return await run_report(self._conn, self.handle, run_id, timeline)

    async def create_schedule(
        self,
        prompt: str,
        *,
        cron: str | None = None,
        timezone: str | None = None,
        interval_ms: int | None = None,
        session_id: str | None = None,
        id: str | None = None,
        queue: str | None = None,
        principal: Principal | None = None,
    ) -> Schedule:
        """Store a schedule whose fires run this agent in the background.

        A worker (`linch-server` with a `worker` section on the same
        database) fires it; this connection never does, and a stdio server
        runs no worker. The agent needs Postgres stores and no host tools or
        callbacks; its config as opened, without the store DSN, is stored
        with the schedule. Fires missed while every worker was down
        coalesce into one run; the rest count in `skips`.

        Args:
            prompt: The user prompt of every fire's run.
            cron: A cron pattern (five fields, or six with seconds); give
                this or `interval_ms`.
            timezone: The IANA zone `cron` is read in; UTC when absent.
            interval_ms: Fire every this many milliseconds instead.
            session_id: The session every fire continues, as a new run there
                (a heartbeat); a fire is skipped while an earlier fire's job
                is unfinished or the session has a live run. None: each fire
                starts its own session.
            id: The schedule id; a fresh uuid when absent.
            queue: The worker queue; `default` when absent.
            principal: Who every fire's run acts for, as in `run`: rules
                with `role` or `unless_role` match its roles. Stored with
                the schedule.

        Returns:
            The stored schedule, with its `next_fire`.

        Raises:
            NotBackground: The agent cannot run in the background.
            InvalidSchedule: The timing is unusable.
            ScheduleExists: The namespace has a schedule `id` already.
            NotAllowed: A tenant without `max_schedules`, or one firing
                faster than its `min_schedule_interval_ms`.
            LimitReached: The tenant holds its `max_schedules`.
        """
        return await _schedule.create(
            self._conn,
            self.handle,
            prompt,
            cron=cron,
            timezone=timezone,
            interval_ms=interval_ms,
            session_id=session_id,
            id=id,
            queue=queue,
            principal=principal,
        )

    async def heartbeat(
        self,
        session_id: str,
        *,
        interval_ms: int | None = None,
        cron: str | None = None,
        timezone: str | None = None,
        prompt: str = CHECK_IN,
        id: str | None = None,
        queue: str | None = None,
        principal: Principal | None = None,
    ) -> Schedule:
        """Schedule periodic check-ins that continue one session.

        `create_schedule` with a fixed `session_id`: each fire is a new run
        in that session prompted with `prompt`, and two never overlap: a
        fire is skipped (counted in `skips`) while the last one's job is
        unfinished or the session has a live run, including one suspended
        for an approval or an answer.
        """
        return await self.create_schedule(
            prompt,
            cron=cron,
            timezone=timezone,
            interval_ms=interval_ms,
            session_id=session_id,
            id=id,
            queue=queue,
            principal=principal,
        )

    async def schedules(self) -> list[Schedule]:
        """Return the schedules of the agent's namespace, by id."""
        return await _schedule.listed(self._conn, self.handle)

    async def pause_schedule(self, id: str) -> Schedule:
        """Stop a schedule's fires; its queued runs stay queued.

        Raises:
            ScheduleNotFound: The namespace has no schedule `id`.
        """
        return await _schedule.by_id(self._conn, "schedule.pause", self.handle, id)

    async def resume_schedule(self, id: str) -> Schedule:
        """Let a paused schedule fire again, from now: the fires it missed
        while paused are not made.

        Raises:
            ScheduleNotFound: The namespace has no schedule `id`.
        """
        return await _schedule.by_id(self._conn, "schedule.resume", self.handle, id)

    async def delete_schedule(self, id: str) -> bool:
        """Delete a schedule; whether it existed. Its queued and running
        runs go on."""
        answer = await _schedule.by_id(self._conn, "schedule.delete", self.handle, id)
        return bool(answer["deleted"])

    async def enqueue(
        self,
        session_id: str,
        prompt: str,
        *,
        queue: str | None = None,
        principal: Principal | None = None,
    ) -> Job:
        """Queue a one-off run of `prompt` in a session, run by a worker.

        `principal` is who the run acts for, as in `run`.

        Raises:
            NotBackground: The agent cannot run in the background.
            NotAllowed: A tenant without `max_queued_jobs`.
            LimitReached: The tenant has that many unfinished jobs.
        """
        return await _schedule.enqueue(
            self._conn, self.handle, session_id, prompt, queue, principal
        )

    async def job(self, job_id: int) -> Job:
        """Return a job of the agent's namespace, live or ended.

        Raises:
            JobNotFound: No such job, or its history was pruned.
        """
        return await _schedule.job(self._conn, self.handle, job_id)

    async def jobs(self, *, schedule_id: str | None = None, limit: int | None = None) -> list[Job]:
        """Return the namespace's jobs newest first, live and ended; only one
        schedule's with `schedule_id`. `limit` defaults to 50, at most 500."""
        return await _schedule.jobs(self._conn, self.handle, schedule_id, limit)

    async def wait_job(
        self, job_id: int, *, poll: float = 0.25, timeout: float | None = None
    ) -> Job:
        """Poll a job until it is `done`, `failed` or `superseded`.

        Raises:
            TimeoutError: `timeout` seconds passed first.
            JobNotFound: No such job.
        """
        return await _schedule.wait(self._conn, self.handle, job_id, poll, timeout)

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
        return Run(self._conn, run_id, queue, observer, self.handle)

    async def _abandon(self, call: asyncio.Future[tuple[str, RunQueue]]) -> None:
        # Cancel the run a cancelled call started, and read it to its end,
        # after which `close` may free the agent.
        try:
            run_id, queue = await call
            run = Run(self._conn, run_id, queue)
            await run.cancel()
        except LinchError:
            return  # Refused, so nothing started; or the connection is gone.
        with contextlib.suppress(LinchError):
            async for _ in run:
                pass


def _answer(answer: Answer) -> dict[str, Any]:
    answers = {
        question: choice if isinstance(choice, str) else list(choice)
        for question, choice in answer.answers.items()
    }
    wire: dict[str, Any] = {"accepted": answer.accepted, "answers": answers}
    if answer.note:
        wire["note"] = answer.note
    return wire


def _decision(decision: Decision) -> dict[str, str]:
    if isinstance(decision, Deny):
        return {"decision": "deny", "reason": decision.reason}
    return {"decision": "allow"}

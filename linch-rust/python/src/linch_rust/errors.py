"""The exception tree: one class per family of `linch-server` error codes."""

from __future__ import annotations

from typing import Any


class LinchError(Exception):
    """Base of every error this client raises."""


class ConnectionClosed(LinchError):
    """The server process exited or the connection closed before an answer
    arrived, or a remote server refused the WebSocket upgrade."""


class RpcError(LinchError):
    """An error object the server answered with.

    Attributes:
        code: The JSON-RPC error code (see the `linch-rpc` crate docs).
        message: What went wrong, with its causes.
        data: Structured details for some codes, else `None`.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message
        self.data = data


class ProtocolError(RpcError):
    """A standard JSON-RPC error (-32700 to -32603), such as invalid params."""


class AgentNotFound(RpcError):
    """The agent handle is unknown to the server (-32001)."""


class RunBusy(RpcError):
    """The server already drives that run (-32002)."""


class Unauthenticated(RpcError):
    """A remote server refused the token (-32003, or HTTP 401 on a WebSocket).

    The server closed the connection.
    """


class NotAllowed(RpcError):
    """A remote server's checks for the tenant refused the request (-32004)."""

    @property
    def kind(self) -> str:
        """The check that refused, such as `namespace` or `workspace`."""
        return _field(self.data, "kind")


class LimitReached(RpcError):
    """A cap on runs, connections or logins is reached (-32005, or HTTP 429).

    Nothing was started; try again once a run or connection ends.
    """

    @property
    def kind(self) -> str:
        """`runs`, `agents`, `connections`, `logins`, `schedules`, `jobs` or
        `provider_calls` (a rate: `max` calls of `provider.models` and
        `provider.model` per minute, so try again later); for a workflow,
        `workflows` (at once), `agent_calls_in_flight` or `journal_records`."""
        return _field(self.data, "kind")

    @property
    def max(self) -> int | None:
        """The cap, when the server named it."""
        try:
            return int(self.data["max"])
        except (TypeError, KeyError, ValueError):
            return None


def _field(data: Any, name: str) -> str:
    try:
        return str(data[name])
    except (TypeError, KeyError):
        return ""


class OpenError(RpcError):
    """The agent could not open: a bad config, provider, store or MCP server
    (-32010 to -32015)."""


class RunError(RpcError):
    """The run could not continue durably (-32020 to -32034 and -32038), or failed
    inside the server (-32603 in `run.end`); it stays resumable."""


class RunExists(RunError):
    """A run with that id is already stored (-32023); nothing was written.

    `Agent.run` reports it when the run ends. `run_or_resume` handles it.
    """


class RunNotFound(RunError):
    """No such run or session in the agent's stores (-32021)."""


class LeaseLost(RunError):
    """Another process took the run over, or failed it, while this one
    drove it (-32022).

    The run stopped here without writing; its new holder drives it. A
    resume from here is refused with `RunLeased` while that holder lives.
    """


class ContractMismatch(RunError):
    """The run was started under another contract (-32025)."""


class RunTerminal(RunError):
    """The run has already finished (-32026)."""


class NotSuspended(RunError):
    """`Agent.approve` on a run not waiting for an approval, or `Agent.answer`
    on one not waiting for input (-32027).

    An approve sent again after its answers were committed (after a crash
    or a dropped connection) gets this and writes nothing; continue the
    run with `Agent.resume`, which keeps the committed decisions.
    """


class InvalidAnswer(RunError):
    """`Agent.answer` with an answer that does not fit the run (-32032).

    No answers, a request that is not pending, or an answer that does not
    fit its questions. Nothing was written: the run still waits for input.
    """

    @property
    def reason(self) -> str:
        """What is wrong with the answer."""
        try:
            return str(self.data["reason"])
        except (TypeError, KeyError):
            return ""


class PromptBlocked(RunError):
    """A `user_prompt_submit` callback refused the prompt (-32029).

    No run or session was created.
    """

    @property
    def reason(self) -> str:
        """The reason the callback gave."""
        try:
            return str(self.data["reason"])
        except (TypeError, KeyError):
            return ""


class InvalidOutputSchema(RunError):
    """The `output_schema` given to `Agent.run` is unusable (-32031): it does
    not compile, or its root is not an object. No run was created.
    """

    @property
    def reason(self) -> str:
        """What is wrong with the schema."""
        try:
            return str(self.data["reason"])
        except (TypeError, KeyError):
            return ""


class InvalidSteer(RunError):
    """`Agent.steer` with text that is blank or too long, or an unusable id (-32038).

    Nothing was queued.
    """

    @property
    def reason(self) -> str:
        """What is wrong with the steer."""
        try:
            return str(self.data["reason"])
        except (TypeError, KeyError):
            return ""


class InvalidToolChoice(RunError):
    """The `tool_choice` given to `Agent.run` is unusable (-32034): it names a
    tool the run does not offer, or the agent's provider refuses it (DeepSeek
    refuses a forced choice while it thinks). No run was created.
    """

    @property
    def reason(self) -> str:
        """What is wrong with the choice."""
        try:
            return str(self.data["reason"])
        except (TypeError, KeyError):
            return ""


class SessionBusy(RunError):
    """The session already has a run that is not terminal (-32030).

    The new run was not created; resume, approve or cancel the live run
    first.
    """

    @property
    def session_id(self) -> str:
        """The busy session."""
        return self._field("session_id")

    @property
    def run_id(self) -> str:
        """The session's live run."""
        return self._field("run_id")

    def _field(self, name: str) -> str:
        try:
            return str(self.data[name])
        except (TypeError, KeyError):
            return ""


class RunLeased(RunError):
    """Another process drives the run and keeps its lease alive (-32033).

    `Agent.resume`, `Agent.approve` or `Agent.answer` wrote nothing. Try
    again once that holder stops: at the latest after `expires_in`
    seconds, when its lease runs out without a renewal. Only runs on
    Postgres stores have leases.
    """

    @property
    def run_id(self) -> str:
        """The run that was asked for."""
        return _field(self.data, "run_id")

    @property
    def owner(self) -> str:
        """The owner name the holder claimed the run under."""
        return _field(self.data, "owner")

    @property
    def expires_in(self) -> float | None:
        """Seconds the holder's lease runs without a renewal, when the
        server said."""
        try:
            return int(self.data["expires_in_ms"]) / 1000
        except (TypeError, KeyError, ValueError):
            return None


class WorkflowError(RpcError):
    """A workflow call was refused or failed (-32045 to -32048)."""


class WorkflowHeld(WorkflowError):
    """Another client drives the workflow (-32045): this connection, another
    connection or another server. `workflow.start` wrote nothing; try again
    once that client finishes or its connection ends."""


class WorkflowBudgetExceeded(WorkflowError):
    """The workflow's budget is spent, so no further agent call starts (-32046).

    The calls that finished stay journaled; raise the budget and start the
    workflow again to continue.
    """

    @property
    def spent_tokens(self) -> int | None:
        """Tokens the workflow's agent calls have spent, replays included."""
        try:
            return int(self.data["spent_tokens"])
        except (TypeError, KeyError, ValueError):
            return None

    @property
    def spent_usd(self) -> float | None:
        """The priced cost of those calls in USD."""
        try:
            return float(self.data["spent_usd"])
        except (TypeError, KeyError, ValueError):
            return None


class WorkflowCallFailed(WorkflowError):
    """A `wf.agent` call did not produce a result (-32047)."""

    @property
    def kind(self) -> str:
        """`failed`, `aborted`, `timeout`, `approval` (the child run stopped
        for an approval) or `invalid` (an unknown subagent)."""
        return _field(self.data, "kind")


class WorkflowNotFound(WorkflowError):
    """This connection drives no workflow with that id (-32048)."""


class SchedulerError(RpcError):
    """A schedule or background-run call was refused (-32040 to -32044)."""


class NotBackground(SchedulerError):
    """The agent cannot run in the background (-32040): `reason` is
    `memory_store`, `host_tools` or `callbacks`."""

    @property
    def reason(self) -> str:
        """Why the agent cannot run without this connection."""
        return _field(self.data, "reason")


class ScheduleNotFound(SchedulerError):
    """The agent's namespace has no schedule with that id (-32041)."""


class ScheduleExists(SchedulerError):
    """The agent's namespace already has a schedule with that id (-32042)."""


class JobNotFound(SchedulerError):
    """The agent's namespace has no job with that id, or its history was
    pruned (-32043)."""


class InvalidSchedule(SchedulerError):
    """The schedule's timing is unusable (-32044): a cron pattern that does
    not parse, an unknown time zone, an interval out of range, or both or
    neither of `cron` and `interval_ms`."""

    @property
    def reason(self) -> str:
        """What is wrong with the timing."""
        return _field(self.data, "reason")


class ProviderInfoError(RpcError):
    """A question about a provider failed (-32035 to -32037): see
    `ModelsUnsupported`, `ModelNotFound` and `ProviderRequestFailed`."""


class ModelsUnsupported(ProviderInfoError):
    """The provider has no model list (-32035), such as `scripted`."""

    @property
    def provider(self) -> str:
        """The provider's id."""
        return _field(self.data, "provider")


class ModelNotFound(ProviderInfoError):
    """The provider's list does not hold the model (-32036)."""

    @property
    def model(self) -> str:
        """The model id asked for."""
        return _field(self.data, "model")

    @property
    def truncated(self) -> bool:
        """The list was cut short, so the model may exist past the cut."""
        try:
            return bool(self.data["truncated"])
        except (TypeError, KeyError):
            return False


class ProviderRequestFailed(ProviderInfoError):
    """The provider could not be asked, or answered with an error (-32037).

    The message is fixed text per `kind`: it names neither the key, the
    provider's URL nor the vendor's own words.
    """

    @property
    def kind(self) -> str:
        """`unreachable` (a refused connection, a reset, a timeout and any
        other transport failure look alike), `status`, `invalid_request` or
        `other`."""
        return _field(self.data, "kind")

    @property
    def status(self) -> int | None:
        """The HTTP status of a `status` failure, else `None`."""
        try:
            return int(self.data["status"])
        except (TypeError, KeyError, ValueError):
            return None


_BY_CODE: dict[int, type[RpcError]] = {
    -32035: ModelsUnsupported,
    -32036: ModelNotFound,
    -32037: ProviderRequestFailed,
    -32040: NotBackground,
    -32041: ScheduleNotFound,
    -32042: ScheduleExists,
    -32043: JobNotFound,
    -32044: InvalidSchedule,
    -32001: AgentNotFound,
    -32002: RunBusy,
    -32003: Unauthenticated,
    -32004: NotAllowed,
    -32005: LimitReached,
    -32021: RunNotFound,
    -32023: RunExists,
    -32045: WorkflowHeld,
    -32046: WorkflowBudgetExceeded,
    -32047: WorkflowCallFailed,
    -32048: WorkflowNotFound,
    -32022: LeaseLost,
    -32025: ContractMismatch,
    -32026: RunTerminal,
    -32027: NotSuspended,
    -32029: PromptBlocked,
    -32030: SessionBusy,
    -32031: InvalidOutputSchema,
    -32032: InvalidAnswer,
    -32033: RunLeased,
    -32034: InvalidToolChoice,
    -32038: InvalidSteer,
}


def error_from(obj: dict[str, Any]) -> RpcError:
    """Build the exception for a JSON-RPC error object `{code, message, data?}`."""
    code = int(obj.get("code", 0))
    cls = _BY_CODE.get(code)
    if cls is None:
        if -32700 <= code <= -32600:
            cls = ProtocolError
        elif -32015 <= code <= -32010:
            cls = OpenError
        elif -32034 <= code <= -32020:
            cls = RunError
        elif -32037 <= code <= -32035:
            cls = ProviderInfoError
        elif -32048 <= code <= -32045:
            cls = WorkflowError
        else:
            cls = RpcError
    return cls(code, str(obj.get("message", "")), obj.get("data"))


def run_end_error(obj: dict[str, Any]) -> RunError:
    """Build the exception for the `error` of a `run.end`.

    A run that stopped with an error always ends with a `RunError`, so a
    run that failed inside the server (-32603), which `error_from` reads as
    a `ProtocolError`, is caught and resumed like any other.
    """
    error = error_from(obj)
    if isinstance(error, RunError):
        return error
    return RunError(error.code, error.message, error.data)

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
        """`runs`, `connections` or `logins`."""
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
    """The run could not continue durably (-32020 to -32030), or failed
    inside the server (-32603 in `run.end`); it stays resumable."""


class RunNotFound(RunError):
    """No such run or session in the agent's stores (-32021)."""


class ContractMismatch(RunError):
    """The run was started under another contract (-32025)."""


class RunTerminal(RunError):
    """The run has already finished (-32026)."""


class NotSuspended(RunError):
    """`Agent.approve` on a run that is not waiting for an approval (-32027).

    An approve sent again after its answers were committed (after a crash
    or a dropped connection) gets this and writes nothing; continue the
    run with `Agent.resume`, which keeps the committed decisions.
    """


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


_BY_CODE: dict[int, type[RpcError]] = {
    -32001: AgentNotFound,
    -32002: RunBusy,
    -32003: Unauthenticated,
    -32004: NotAllowed,
    -32005: LimitReached,
    -32021: RunNotFound,
    -32025: ContractMismatch,
    -32026: RunTerminal,
    -32027: NotSuspended,
    -32029: PromptBlocked,
    -32030: SessionBusy,
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
        elif -32030 <= code <= -32020:
            cls = RunError
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

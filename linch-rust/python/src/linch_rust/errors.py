"""The exception tree: one class per family of `linch-server` error codes."""

from __future__ import annotations

from typing import Any


class LinchError(Exception):
    """Base of every error this client raises."""


class ConnectionClosed(LinchError):
    """The server process exited or its pipes closed before an answer arrived."""


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
    -32021: RunNotFound,
    -32025: ContractMismatch,
    -32026: RunTerminal,
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

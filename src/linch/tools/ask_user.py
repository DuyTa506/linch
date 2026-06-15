from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..abort import throw_if_aborted
from ..errors import AbortError
from .base import ToolContext, ToolResult, ToolScope


@dataclass(frozen=True, slots=True)
class AskUserOption:
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class AskUserQuestion:
    id: str
    header: str
    question: str
    options: list[AskUserOption]
    multi_select: bool = False


@dataclass(frozen=True, slots=True)
class AskUserRequest:
    questions: list[AskUserQuestion]


@dataclass(frozen=True, slots=True)
class AskUserResponse:
    accepted: bool
    answers: dict[str, str | list[str]] = field(default_factory=dict)
    note: str = ""


class AskUserHandler(Protocol):
    def __call__(
        self,
        request: AskUserRequest,
        ctx: ToolContext,
    ) -> AskUserResponse | dict[str, Any] | Any: ...


def _option_from_raw(raw: Any) -> AskUserOption:
    if not isinstance(raw, dict):
        raise ValueError("options entries must be objects")
    label = raw.get("label")
    description = raw.get("description")
    if not isinstance(label, str) or label == "":
        raise ValueError("option label must be a non-empty string")
    if not isinstance(description, str) or description == "":
        raise ValueError("option description must be a non-empty string")
    return AskUserOption(label=label, description=description)


def _question_from_raw(raw: Any) -> AskUserQuestion:
    if not isinstance(raw, dict):
        raise ValueError("questions entries must be objects")
    qid = raw.get("id")
    header = raw.get("header")
    question = raw.get("question")
    if not isinstance(qid, str) or qid == "":
        raise ValueError("question id must be a non-empty string")
    if not isinstance(header, str) or header == "":
        raise ValueError("question header must be a non-empty string")
    if len(header) > 12:
        raise ValueError("question header must be 12 characters or fewer")
    if not isinstance(question, str) or question == "":
        raise ValueError("question must be a non-empty string")
    raw_options = raw.get("options")
    if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 4:
        raise ValueError("questions must include 2-4 options")
    options = [_option_from_raw(item) for item in raw_options]
    labels = [option.label for option in options]
    if len(labels) != len(set(labels)):
        raise ValueError("question option labels must be unique")
    return AskUserQuestion(
        id=qid,
        header=header,
        question=question,
        options=options,
        multi_select=bool(raw.get("multi_select", False)),
    )


def _response_from_value(value: Any) -> AskUserResponse:
    if isinstance(value, AskUserResponse):
        return value
    if isinstance(value, dict):
        answers_raw = value.get("answers", {})
        answers = dict(answers_raw) if isinstance(answers_raw, dict) else {}
        accepted = value.get("accepted")
        if accepted is None:
            accepted = value.get("ok", value.get("confirmed"))
        if accepted is None:
            # No explicit acceptance signal: infer from whether the user
            # actually provided answers; otherwise fail closed (declined) rather
            # than fabricating consent from a malformed/empty handler response.
            accepted = bool(answers)
        note = value.get("note", "")
        return AskUserResponse(
            accepted=bool(accepted),
            answers=answers,
            note=str(note) if isinstance(note, str) else "",
        )
    # Unrecognised/None handler response: fail closed — never treat as consent.
    return AskUserResponse(accepted=False, answers={})


class AskUserTool:
    name = "AskUser"
    description = "Ask the user to choose from short, explicit options before proceeding."
    scope: ToolScope = "read"
    parallel = False
    input_schema = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "header": {"type": "string", "maxLength": 12},
                        "question": {"type": "string"},
                        "multi_select": {"type": "boolean"},
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label", "description"],
                            },
                        },
                    },
                    "required": ["id", "header", "question", "options"],
                },
            }
        },
        "required": ["questions"],
    }

    def __init__(
        self,
        handler: AskUserHandler | Callable[..., Any],
        *,
        timeout_s: float | None = None,
    ) -> None:
        self._handler = handler
        self._timeout_s = timeout_s

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        questions_raw = raw.get("questions")
        if not isinstance(questions_raw, list) or not 1 <= len(questions_raw) <= 4:
            raise ValueError("AskUser requires 1-4 questions")
        questions = [_question_from_raw(item) for item in questions_raw]
        ids = [question.id for question in questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question ids must be unique")
        return {"questions": questions}

    def summarize(self, input: dict[str, Any]) -> str:
        return f"AskUser({len(input.get('questions', []))} question(s))"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        throw_if_aborted(ctx.signal)
        request = AskUserRequest(questions=list(input["questions"]))
        handler_task = asyncio.create_task(self._call_handler(request, ctx))
        abort_task: asyncio.Task[Any] | None = None
        if ctx.signal is not None:
            abort_task = asyncio.create_task(ctx.signal.wait())
        try:
            wait_set = {handler_task}
            if abort_task is not None:
                wait_set.add(abort_task)
            done, _ = await asyncio.wait(
                wait_set,
                timeout=self._timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if abort_task is not None and abort_task in done:
                handler_task.cancel()
                raise AbortError("aborted")
            if handler_task in done:
                response = _response_from_value(await handler_task)
            else:
                # Timed out waiting for the user: decline rather than hang the
                # turn (and the run) on an unanswered prompt.
                handler_task.cancel()
                response = AskUserResponse(accepted=False, note="AskUser timed out")
        finally:
            if abort_task is not None and not abort_task.done():
                abort_task.cancel()

        if not response.accepted:
            content = (
                "The user declined to answer. Proceed with explicit assumptions, "
                "state them briefly, and continue without asking again."
            )
            if response.note:
                content += f"\nUser note: {response.note}"
            return ToolResult(content=content, summary="User declined AskUser")
        return ToolResult(
            content=json.dumps(
                {
                    "answers": response.answers,
                    **({"note": response.note} if response.note else {}),
                }
            ),
            summary="User answered AskUser",
        )

    async def _call_handler(self, request: AskUserRequest, ctx: ToolContext) -> Any:
        if inspect.iscoroutinefunction(self._handler):
            return await self._handler(request, ctx)
        # Sync (or unknown) callable: offload the call to a bounded worker thread
        # so a blocking handler (stdin read, GUI prompt) never stalls the event
        # loop and its concurrent tools/background workers.
        from .._blocking import run_blocking

        value = await run_blocking(self._handler, request, ctx)
        if inspect.isawaitable(value):
            return await value
        return value

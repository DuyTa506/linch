"""The ``send_message`` tool: address a message to a peer's mailbox.

A thin mechanism over :class:`~linch.mailbox.Mailbox`. The agent (parent or
worker) names a recipient address; the message lands in that recipient's inbox
and is drained into its ``provider_view`` on its next turn. Message *semantics*
are embedder choreography — this tool only moves bytes between inboxes.
"""

from __future__ import annotations

from typing import Any

from ..tools.base import ToolContext, ToolResult, ToolScope

SEND_MESSAGE_TOOL_NAME = "send_message"

SEND_MESSAGE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": "Address of the recipient (a worker display name or session handle).",
        },
        "content": {
            "type": "string",
            "description": "The message body to deliver to the recipient.",
        },
        "type": {
            "type": "string",
            "description": "Optional neutral category for the message (default 'message').",
        },
        "request_id": {
            "type": "string",
            "description": "Optional correlation id when this message expects a reply.",
        },
        "in_reply_to": {
            "type": "string",
            "description": "Optional request_id this message is a response to.",
        },
    },
    "required": ["to", "content"],
}


class SendMessageTool:
    name = SEND_MESSAGE_TOOL_NAME
    input_schema = SEND_MESSAGE_TOOL_SCHEMA
    scope: ToolScope = "exec"
    parallel = True

    def __init__(self, mailbox: Any, get_session: Any) -> None:
        self._mailbox = mailbox
        self._get_session = get_session

    @property
    def description(self) -> str:
        return "\n".join(
            [
                "Send a message to a peer agent's mailbox by address.",
                "",
                "The recipient receives it at the top of its next turn. Use 'request_id'",
                "to mark a message that expects a reply, and 'in_reply_to' to answer one.",
                "Message meaning is up to you — this only delivers the content.",
            ]
        )

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        to = raw.get("to")
        if not isinstance(to, str) or to.strip() == "":
            raise ValueError("'to' must be a non-empty recipient address")
        content = raw.get("content")
        if not isinstance(content, str) or content == "":
            raise ValueError("'content' must be a non-empty string")
        out: dict[str, object] = {"to": to.strip(), "content": content}
        for key in ("type", "request_id", "in_reply_to"):
            value = raw.get(key)
            if isinstance(value, str) and value != "":
                out[key] = value
        return out

    def summarize(self, input: dict[str, object]) -> str:
        return f"Send message to {input.get('to', '?')}"

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        from .mailbox import MailboxMessage

        recipient = str(input["to"]).strip()
        session = self._get_session(ctx.session_id)
        sender = getattr(session, "mailbox_address", None) or ctx.session_id

        message = MailboxMessage(
            sender=sender,
            recipient=recipient,
            content=str(input["content"]),
            type=str(input.get("type", "message")),
            request_id=_opt_str(input.get("request_id")),
            in_reply_to=_opt_str(input.get("in_reply_to")),
        )
        await self._mailbox.send(message)
        return ToolResult(
            content=f"Message delivered to '{recipient}'.",
            summary=self.summarize(input),
        )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value != "" else None

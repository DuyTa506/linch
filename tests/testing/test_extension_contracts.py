from __future__ import annotations

from typing import Any


async def test_tool_contract_accepts_decorated_function_tool() -> None:
    from linch import assert_tool_contract, tool

    @tool
    def echo(value: str) -> str:
        """Echo a value."""
        return f"echo:{value}"

    result = await assert_tool_contract(
        echo,
        valid_input={"value": "ok"},
        invalid_input={},
    )

    assert result.content == "echo:ok"


async def test_tool_contract_accepts_input_aware_tool() -> None:
    from linch import ResourceAccess, ToolContext, ToolResult, assert_tool_contract

    class ModalTool:
        name = "Modal"
        description = "Run in read or write mode."
        input_schema = {
            "type": "object",
            "properties": {
                "mode": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["mode", "value"],
        }
        scope = "exec"

        def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
            mode = raw.get("mode")
            value = raw.get("value")
            if mode not in {"read", "write"}:
                raise ValueError("mode must be read or write")
            if not isinstance(value, str) or value == "":
                raise ValueError("value must be a non-empty string")
            return {"mode": mode, "value": value}

        def parallel(self, input: dict[str, Any]) -> bool:
            return input["mode"] == "read"

        def resources(self, input: dict[str, Any]) -> list[ResourceAccess]:
            return [ResourceAccess(resource=f"modal:{input['value']}", mode=input["mode"])]

        def summarize(self, input: dict[str, Any]) -> str:
            return f"Modal {input['mode']}"

        async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
            return ToolResult(content=f"{ctx.session_id}:{input['value']}")

    result = await assert_tool_contract(
        ModalTool(),
        valid_input={"mode": "read", "value": "alpha"},
        invalid_input={"mode": "delete", "value": "alpha"},
    )

    assert result.content == "contract-session:alpha"


async def test_tool_contract_rejects_tool_with_only_legacy_schema_attribute() -> None:
    import pytest

    from linch import ToolResult, assert_tool_contract

    class LegacySchemaTool:
        name = "LegacySchema"
        description = "Legacy schema test tool."
        schema = {"type": "object"}
        scope = "read"
        parallel = True

        def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
            return dict(raw)

        def summarize(self, input: dict[str, Any]) -> str:
            return self.name

        async def execute(self, input: dict[str, Any], ctx: Any) -> ToolResult:
            return ToolResult(content="ok")

    with pytest.raises(AssertionError, match="input_schema"):
        await assert_tool_contract(LegacySchemaTool(), valid_input={})


async def test_file_backend_contract_accepts_state_backend() -> None:
    from linch import StateFileBackend, assert_file_backend_contract

    await assert_file_backend_contract(StateFileBackend)


async def test_file_backend_contract_accepts_postgres_backend(
    pg_dsn,
) -> None:
    from linch import assert_file_backend_contract
    from linch.filesystem.postgres import PostgresFileBackend

    await assert_file_backend_contract(lambda: PostgresFileBackend(pg_dsn))


async def test_file_backend_contract_accepts_disk_backend(tmp_path) -> None:
    from linch import DiskFileBackend, assert_file_backend_contract

    await assert_file_backend_contract(lambda: DiskFileBackend(tmp_path / "files"))


async def test_isolation_backend_contract_accepts_tempdir_isolation(tmp_path) -> None:
    from linch import TempDirIsolation, assert_isolation_backend_contract

    await assert_isolation_backend_contract(lambda: TempDirIsolation(root=str(tmp_path)))


async def test_mailbox_contract_accepts_in_memory_mailbox() -> None:
    from linch import InMemoryMailbox, assert_mailbox_contract

    await assert_mailbox_contract(InMemoryMailbox)


async def test_mailbox_contract_rejects_lossy_message_fields() -> None:
    from dataclasses import replace

    import pytest

    from linch import MailboxMessage, assert_mailbox_contract

    class LossyMailbox:
        def __init__(self) -> None:
            self._pending: list[MailboxMessage] = []

        async def send(self, message: MailboxMessage) -> None:
            self._pending.append(message)

        async def drain(self, recipient: str) -> list[MailboxMessage]:
            matched = [message for message in self._pending if message.recipient == recipient]
            self._pending = [message for message in self._pending if message.recipient != recipient]
            return [replace(message, content="corrupted") for message in matched]

    with pytest.raises(AssertionError, match="message"):
        await assert_mailbox_contract(LossyMailbox)


def test_contract_helpers_are_public_top_level_api() -> None:
    import linch

    assert "assert_file_backend_contract" in linch.__all__
    assert "assert_isolation_backend_contract" in linch.__all__
    assert "assert_mailbox_contract" in linch.__all__
    assert "assert_tool_contract" in linch.__all__

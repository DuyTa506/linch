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


async def test_file_backend_contract_accepts_state_backend() -> None:
    from linch import StateFileBackend, assert_file_backend_contract

    await assert_file_backend_contract(StateFileBackend)


async def test_file_backend_contract_accepts_sqlite_backend(tmp_path) -> None:
    from linch import SqliteFileBackend, assert_file_backend_contract

    await assert_file_backend_contract(lambda: SqliteFileBackend(tmp_path / "files.db"))


async def test_file_backend_contract_accepts_disk_backend(tmp_path) -> None:
    from linch import DiskFileBackend, assert_file_backend_contract

    await assert_file_backend_contract(lambda: DiskFileBackend(tmp_path / "files"))


async def test_isolation_backend_contract_accepts_tempdir_isolation(tmp_path) -> None:
    from linch import TempDirIsolation, assert_isolation_backend_contract

    await assert_isolation_backend_contract(lambda: TempDirIsolation(root=str(tmp_path)))


async def test_mailbox_contract_accepts_in_memory_mailbox() -> None:
    from linch import InMemoryMailbox, assert_mailbox_contract

    await assert_mailbox_contract(InMemoryMailbox)


async def test_mailbox_contract_accepts_sqlite_mailbox(tmp_path) -> None:
    from linch import SqliteMailbox, assert_mailbox_contract

    await assert_mailbox_contract(lambda: SqliteMailbox(tmp_path / "mailbox.db"))


async def test_memory_store_contract_accepts_keyword_store() -> None:
    from linch import InMemoryKeywordMemoryStore, assert_memory_store_contract

    await assert_memory_store_contract(InMemoryKeywordMemoryStore)


async def test_memory_store_contract_accepts_sqlite_store(tmp_path) -> None:
    from linch import SqliteMemoryStore, assert_memory_store_contract

    await assert_memory_store_contract(lambda: SqliteMemoryStore(tmp_path / "memory.db"))


async def test_schedule_store_contract_accepts_in_memory_store() -> None:
    from linch import InMemoryScheduleStore, assert_schedule_store_contract

    await assert_schedule_store_contract(InMemoryScheduleStore)


async def test_schedule_store_contract_accepts_sqlite_store(tmp_path) -> None:
    from linch import SqliteScheduleStore, assert_schedule_store_contract

    await assert_schedule_store_contract(lambda: SqliteScheduleStore(tmp_path / "schedules.db"))


def test_contract_helpers_are_public_top_level_api() -> None:
    import linch

    assert "assert_file_backend_contract" in linch.__all__
    assert "assert_isolation_backend_contract" in linch.__all__
    assert "assert_mailbox_contract" in linch.__all__
    assert "assert_memory_store_contract" in linch.__all__
    assert "assert_schedule_store_contract" in linch.__all__
    assert "assert_tool_contract" in linch.__all__

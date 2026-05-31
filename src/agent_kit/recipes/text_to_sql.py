"""Text-to-SQL recipe.

Creates an agent that converts natural-language questions into SQL queries,
runs them against a database, and returns a structured ``{sql, rationale}``
result.

Exercises: ``final_tool_name``, :class:`~agent_kit.types.OutputSchema`,
``deps``, tool-aware system prompt.

Quick start::

    import sqlite3
    from agent_kit.recipes.text_to_sql import sql_agent, SqlDeps

    conn = sqlite3.connect("mydb.sqlite")
    schema = "CREATE TABLE orders (id INT, amount FLOAT, status TEXT);"

    agent = sql_agent(
        model="gpt-5",
        schema=schema,
        deps=SqlDeps(db=conn),
    )

    session = await agent.session()
    async for event in session.run("How many orders are pending?"):
        if event.type == "result":
            print(event.structured_output)
            # {'sql': 'SELECT COUNT(*) FROM orders WHERE status = \\'pending\\'',
            #  'rationale': '...'}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..agent import Agent
from ..tools.base import ToolContext, ToolResult, ToolScope
from ..tools.registry import empty_tools
from ..types import OutputSchema
from . import build_agent

_SQL_RESULT_SCHEMA = OutputSchema(
    name="sql_result",
    schema={
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "The SQL query to execute."},
            "rationale": {
                "type": "string",
                "description": "Brief explanation of why this query answers the question.",
            },
        },
        "required": ["sql", "rationale"],
        "additionalProperties": False,
    },
    strict=True,
)


@dataclass
class SqlDeps:
    """Dependency container for the text-to-SQL recipe.

    Attributes:
        db: Any database connection that supports ``cursor().execute(sql)``
            and ``fetchall()``.  Tested with ``sqlite3`` connections.
    """

    db: Any


class _RunSqlTool:
    """Execute a SQL query and return the results as a string."""

    name = "RunSQL"
    description = (
        "Execute a read-only SQL SELECT query and return the results. "
        "Use this to verify that your query produces the expected output "
        "before finalising it."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A SQL SELECT statement to execute."},
        },
        "required": ["sql"],
    }
    scope: ToolScope = "read"
    parallel_safe: bool = False

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        sql = raw.get("sql", "")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sql must be a non-empty string")
        stripped = sql.strip().upper()
        if not stripped.startswith("SELECT"):
            raise ValueError("only SELECT queries are allowed via RunSQL")
        return {"sql": sql}

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        deps: SqlDeps | None = ctx.deps if isinstance(ctx.deps, SqlDeps) else None
        if deps is None or deps.db is None:
            return ToolResult(
                content="No database connection available.",
                summary="RunSQL(no db)",
                is_error=True,
            )
        try:
            cursor = deps.db.cursor()
            cursor.execute(input["sql"])
            rows = cursor.fetchall()
            text = "\n".join(str(row) for row in rows[:50])  # cap at 50 rows
            if len(rows) > 50:
                text += f"\n... ({len(rows) - 50} more rows)"
            return ToolResult(content=text or "(no rows)", summary=f"RunSQL({input['sql'][:60]})")
        except Exception as exc:
            return ToolResult(
                content=f"Query error: {exc}",
                summary="RunSQL(error)",
                is_error=True,
            )

    def summarize(self, input: dict[str, Any]) -> str:
        sql = input.get("sql", "")
        return f"RunSQL({sql[:60]}{'...' if len(sql) > 60 else ''})"


def sql_agent(
    *,
    model: str,
    schema: str,
    deps: SqlDeps | None = None,
    output_schema: OutputSchema | None = None,
    extra_instructions: str | None = None,
    allow_run_sql: bool = True,
    **agent_kwargs: Any,
) -> Agent:
    """Create a text-to-SQL agent.

    Args:
        model: LLM model identifier.
        schema: Database schema (DDL) injected into the system prompt.
            The model uses it to generate correct column/table names.
        deps: :class:`SqlDeps` containing the database connection.
        output_schema: Override the default ``{sql, rationale}`` schema.
        extra_instructions: Additional system instructions.
        allow_run_sql: If ``True`` (default), include a ``RunSQL`` tool so
            the model can verify its queries before the final answer.
        **agent_kwargs: Forwarded to :func:`~agent_kit.recipes.build_agent`.

    Returns:
        An :class:`~agent_kit.agent.Agent` that terminates on the
        ``emit_sql`` tool call with ``structured_output`` populated.
    """
    base_instructions = (
        f"You are an expert SQL assistant.  Convert the user's natural-language "
        f"question into a valid SQL query for the database described below.\n\n"
        f"Database schema:\n```sql\n{schema}\n```\n\n"
        f"When you are confident in your answer, call the `emit_sql` tool with "
        f"the final SQL query and a brief rationale.  Do NOT return a plain text "
        f"answer — always finish by calling `emit_sql`."
    )
    if extra_instructions:
        base_instructions = f"{base_instructions}\n\n{extra_instructions}"

    # Build the terminal "emit_sql" tool that signals the final answer.
    # Its input_schema IS the output schema so the model fills it directly.
    eff_schema = output_schema or _SQL_RESULT_SCHEMA

    class _EmitSqlTool:
        name = "emit_sql"
        description = (
            "Emit the final SQL query and rationale.  Call this ONCE when you "
            "are confident in your answer.  The loop will terminate and return "
            "your input as the structured result."
        )
        input_schema: dict[str, Any] = eff_schema.schema
        scope: ToolScope = "read"
        parallel_safe: bool = False

        def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
            return raw

        async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
            # This should never be reached — the loop intercepts it as a
            # final_tool.  Return a no-op just in case.
            return ToolResult(content=str(input), summary="emit_sql")

        def summarize(self, input: dict[str, Any]) -> str:
            return f"emit_sql({input.get('sql', '')[:60]})"

    registry = empty_tools(_EmitSqlTool())
    if allow_run_sql:
        registry.register(_RunSqlTool())

    return build_agent(
        model=model,
        system_instructions=base_instructions,
        tools=registry,
        output_schema=eff_schema,
        final_tool_name="emit_sql",
        deps=deps,
        replace_default_system=True,
        **agent_kwargs,
    )

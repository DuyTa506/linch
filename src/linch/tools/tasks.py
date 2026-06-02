from __future__ import annotations

from linch.sessions.tasks import CreateTaskInput, TaskPatch
from linch.tools.base import ToolContext, ToolResult, ToolScope, require_str


class TaskCreateTool:
    name = "TaskCreate"
    description = "Create a new persistent task in the current session."
    input_schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Brief task title."},
            "description": {"type": "string", "description": "What needs to be done."},
            "active_form": {
                "type": "string",
                "description": "Present-continuous label shown in spinner.",
            },
            "metadata": {"type": "object", "description": "Arbitrary key-value metadata."},
        },
        "required": ["subject", "description"],
    }
    scope: ToolScope = "write"
    parallel_safe: bool = True

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        subject = require_str(raw, "subject").strip()
        description = require_str(raw, "description")
        active_form = raw.get("active_form")
        if active_form is not None and not isinstance(active_form, str):
            raise ValueError("active_form must be a string")
        metadata = raw.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        return {
            "subject": subject,
            "description": description,
            "active_form": active_form,
            "metadata": metadata,
        }

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        task = await ctx.session_store.create_task(
            ctx.session_id,
            CreateTaskInput(
                subject=str(input["subject"]),
                description=str(input["description"]),
                active_form=input.get("active_form")
                if isinstance(input.get("active_form"), str)
                else None,
                metadata=input.get("metadata") if isinstance(input.get("metadata"), dict) else None,
            ),
        )
        return ToolResult(
            content=f"Task #{task.id} created: {task.subject}",
            summary=self.summarize(input),
        )

    def summarize(self, input: dict[str, object]) -> str:
        return f"Create task: {input['subject']}"


class TaskListTool:
    name = "TaskList"
    description = "List all tasks for the current session."
    input_schema = {"type": "object", "properties": {}}
    scope: ToolScope = "read"
    parallel_safe: bool = True

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        return {}

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        tasks = await ctx.session_store.list_tasks(ctx.session_id)
        if not tasks:
            return ToolResult(content="No tasks found.", summary="List tasks")
        rows = [f"#{task.id} [{task.status}] {task.subject}" for task in tasks]
        return ToolResult(content="\n".join(rows), summary=f"{len(tasks)} tasks")

    def summarize(self, input: dict[str, object]) -> str:
        return "List tasks"


class TaskGetTool:
    name = "TaskGet"
    description = "Get one task by id."
    input_schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }
    scope: ToolScope = "read"
    parallel_safe: bool = True

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        return {"id": require_str(raw, "id")}

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        task = await ctx.session_store.get_task(ctx.session_id, str(input["id"]))
        if task is None:
            return ToolResult(
                content="Task not found.",
                summary=f"Get task {input['id']}",
                is_error=True,
            )
        return ToolResult(
            content=f"#{task.id} [{task.status}] {task.subject}\n{task.description}",
            summary=f"Get task {input['id']}",
        )

    def summarize(self, input: dict[str, object]) -> str:
        return f"Get task {input['id']}"


class TaskUpdateTool:
    name = "TaskUpdate"
    description = "Patch one task."
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "subject": {"type": "string"},
            "description": {"type": "string"},
            "active_form": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "deleted"],
            },
            "owner": {"type": "string"},
            "add_blocks": {"type": "array", "items": {"type": "string"}},
            "add_blocked_by": {"type": "array", "items": {"type": "string"}},
            "remove_blocks": {"type": "array", "items": {"type": "string"}},
            "remove_blocked_by": {"type": "array", "items": {"type": "string"}},
            "metadata": {"type": "object"},
        },
        "required": ["id"],
    }
    scope: ToolScope = "write"
    parallel_safe: bool = True

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        out: dict[str, object] = {"id": require_str(raw, "id")}
        for key in (
            "subject",
            "description",
            "active_form",
            "status",
            "owner",
        ):
            val = raw.get(key)
            if val is not None:
                if not isinstance(val, str):
                    raise ValueError(f"{key} must be a string")
                out[key] = val
        for key in ("add_blocks", "add_blocked_by", "remove_blocks", "remove_blocked_by"):
            val = raw.get(key)
            if val is not None:
                if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
                    raise ValueError(f"{key} must be a list of strings")
                out[key] = val
        if raw.get("metadata") is not None:
            if not isinstance(raw["metadata"], dict):
                raise ValueError("metadata must be an object")
            out["metadata"] = raw["metadata"]
        return out

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        patch = TaskPatch(
            subject=(input.get("subject") if isinstance(input.get("subject"), str) else None),
            description=(
                input.get("description") if isinstance(input.get("description"), str) else None
            ),
            active_form=(
                input.get("active_form") if isinstance(input.get("active_form"), str) else None
            ),
            status=input.get("status") if isinstance(input.get("status"), str) else None,
            owner=input.get("owner") if isinstance(input.get("owner"), str) else None,
            add_blocks=(
                input.get("add_blocks") if isinstance(input.get("add_blocks"), list) else None
            ),
            add_blocked_by=(
                input.get("add_blocked_by")
                if isinstance(input.get("add_blocked_by"), list)
                else None
            ),
            remove_blocks=(
                input.get("remove_blocks") if isinstance(input.get("remove_blocks"), list) else None
            ),
            remove_blocked_by=(
                input.get("remove_blocked_by")
                if isinstance(input.get("remove_blocked_by"), list)
                else None
            ),
            metadata=(input.get("metadata") if isinstance(input.get("metadata"), dict) else None),
        )
        task = await ctx.session_store.update_task(ctx.session_id, str(input["id"]), patch)
        if task is None and patch.status == "deleted":
            return ToolResult(
                content=f"Task #{input['id']} deleted.",
                summary=self.summarize(input),
            )
        if task is None:
            return ToolResult(
                content="Task not found.",
                summary=self.summarize(input),
                is_error=True,
            )
        return ToolResult(
            content=f"Task #{task.id} updated: [{task.status}] {task.subject}",
            summary=self.summarize(input),
        )

    def summarize(self, input: dict[str, object]) -> str:
        return f"Update task {input['id']}"

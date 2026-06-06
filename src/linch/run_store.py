from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from .events import Event, event_from_dict, event_to_dict, usage_from_dict, usage_to_dict
from .sessions.memory import now_iso
from .storage._executor import SqliteExecutor
from .types import (
    Message,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    block_from_dict,
    block_to_dict,
    message_from_dict,
    message_to_dict,
)

RunStatus = Literal["running", "waiting_permission", "completed", "failed", "aborted"]
RunPhase = Literal[
    "started",
    "user_appended",
    "provider_pending",
    "assistant_appended",
    "permission_pending",
    "tool_batch_pending",
    "tool_results_appended",
    "turn_complete",
    "completed",
    "failed",
    "aborted",
]


@dataclass(slots=True)
class RunCheckpoint:
    phase: RunPhase
    prompt: str
    turn_index: int
    total_usage: Usage
    assistant_message: Message | None = None
    pending_tool_blocks: list[ToolUseBlock] = field(default_factory=list)
    completed_tool_results: dict[str, ToolResultBlock] = field(default_factory=dict)
    force_final_pending: bool = False
    loop_guard_state: dict[str, object] | None = None
    pending_skill_overlay: dict[str, object] | None = None
    current_turn_allowed_tools: list[str] | None = None
    assistant_stop_reason: str | None = None


@dataclass(slots=True)
class RunRecord:
    id: str
    session_id: str
    status: RunStatus
    created_at: str
    updated_at: str
    checkpoint: RunCheckpoint | None = None
    meta: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class StoredRunEvent:
    seq: int
    appended_at: str
    event: Event


class RunStore(Protocol):
    async def create_run(
        self,
        session_id: str,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> RunRecord: ...

    async def load_run(self, run_id: str) -> RunRecord | None: ...

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        *,
        status: str = "running",
    ) -> RunRecord: ...

    async def append_event(self, run_id: str, event: Event) -> int: ...

    async def load_events(self, run_id: str, *, after_seq: int = 0) -> list[StoredRunEvent]: ...

    async def mark_completed(self, run_id: str, checkpoint: RunCheckpoint) -> RunRecord: ...

    async def mark_failed(
        self,
        run_id: str,
        checkpoint: RunCheckpoint | None = None,
        error: dict[str, object] | None = None,
    ) -> RunRecord: ...


def checkpoint_to_dict(checkpoint: RunCheckpoint) -> dict[str, Any]:
    return {
        "phase": checkpoint.phase,
        "prompt": checkpoint.prompt,
        "turn_index": checkpoint.turn_index,
        "total_usage": usage_to_dict(checkpoint.total_usage),
        "assistant_message": (
            message_to_dict(checkpoint.assistant_message)
            if checkpoint.assistant_message is not None
            else None
        ),
        "pending_tool_blocks": [block_to_dict(block) for block in checkpoint.pending_tool_blocks],
        "completed_tool_results": {
            key: block_to_dict(block) for key, block in checkpoint.completed_tool_results.items()
        },
        "force_final_pending": checkpoint.force_final_pending,
        "loop_guard_state": checkpoint.loop_guard_state,
        "pending_skill_overlay": checkpoint.pending_skill_overlay,
        "current_turn_allowed_tools": checkpoint.current_turn_allowed_tools,
        "assistant_stop_reason": checkpoint.assistant_stop_reason,
    }


def _dict_or_none(value: Any) -> dict[str, object] | None:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else None


def checkpoint_from_dict(raw: dict[str, Any]) -> RunCheckpoint:
    assistant_raw = raw.get("assistant_message")
    pending_blocks: list[ToolUseBlock] = []
    for item in raw.get("pending_tool_blocks", []):
        if isinstance(item, dict):
            block = block_from_dict(item)
            if isinstance(block, ToolUseBlock):
                pending_blocks.append(block)

    completed: dict[str, ToolResultBlock] = {}
    completed_raw = raw.get("completed_tool_results", {})
    if isinstance(completed_raw, dict):
        for key, item in completed_raw.items():
            if isinstance(item, dict):
                block = block_from_dict(item)
                if isinstance(block, ToolResultBlock):
                    completed[str(key)] = block

    return RunCheckpoint(
        phase=raw.get("phase", "started"),
        prompt=str(raw.get("prompt", "")),
        turn_index=int(raw.get("turn_index", 0) or 0),
        total_usage=usage_from_dict(dict(raw.get("total_usage", {}))),
        assistant_message=(
            message_from_dict(dict(assistant_raw)) if isinstance(assistant_raw, dict) else None
        ),
        pending_tool_blocks=pending_blocks,
        completed_tool_results=completed,
        force_final_pending=bool(raw.get("force_final_pending", False)),
        loop_guard_state=_dict_or_none(raw.get("loop_guard_state")),
        pending_skill_overlay=_dict_or_none(raw.get("pending_skill_overlay")),
        current_turn_allowed_tools=(
            [str(t) for t in raw.get("current_turn_allowed_tools", [])]
            if isinstance(raw.get("current_turn_allowed_tools"), list)
            else None
        ),
        assistant_stop_reason=(
            str(raw.get("assistant_stop_reason"))
            if isinstance(raw.get("assistant_stop_reason"), str)
            else None
        ),
    )


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._events: dict[str, list[StoredRunEvent]] = {}

    async def create_run(
        self,
        session_id: str,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> RunRecord:
        rid = id or str(uuid4())
        existing = self._runs.get(rid)
        if existing is not None:
            return existing
        ts = now_iso()
        record = RunRecord(
            id=rid,
            session_id=session_id,
            status="running",
            created_at=ts,
            updated_at=ts,
            meta=dict(meta or {}),
        )
        self._runs[rid] = record
        self._events[rid] = []
        return record

    async def load_run(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        *,
        status: str = "running",
    ) -> RunRecord:
        rec = self._runs[run_id]
        rec.checkpoint = checkpoint
        rec.status = status  # type: ignore[assignment]
        rec.updated_at = now_iso()
        return rec

    async def append_event(self, run_id: str, event: Event) -> int:
        if run_id not in self._runs:
            raise KeyError(f"run not found: {run_id}")
        bucket = self._events.setdefault(run_id, [])
        seq = len(bucket) + 1
        bucket.append(StoredRunEvent(seq=seq, appended_at=now_iso(), event=event))
        return seq

    async def load_events(self, run_id: str, *, after_seq: int = 0) -> list[StoredRunEvent]:
        return [row for row in self._events.get(run_id, []) if row.seq > after_seq]

    async def mark_completed(self, run_id: str, checkpoint: RunCheckpoint) -> RunRecord:
        checkpoint.phase = "completed"
        return await self.save_checkpoint(run_id, checkpoint, status="completed")

    async def mark_failed(
        self,
        run_id: str,
        checkpoint: RunCheckpoint | None = None,
        error: dict[str, object] | None = None,
    ) -> RunRecord:
        rec = self._runs[run_id]
        if checkpoint is not None:
            checkpoint.phase = "failed"
            rec.checkpoint = checkpoint
        if error is not None:
            rec.meta.setdefault("errors", [])
            errors = rec.meta["errors"]
            if isinstance(errors, list):
                errors.append(error)
        rec.status = "failed"
        rec.updated_at = now_iso()
        return rec

    async def close(self) -> None:
        return None


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists runs (
          id text primary key,
          session_id text not null,
          status text not null,
          created_at text not null,
          updated_at text not null,
          checkpoint text,
          meta text not null
        );
        create table if not exists run_events (
          run_id text not null,
          seq integer not null,
          appended_at text not null,
          event text not null,
          primary key (run_id, seq)
        );
        """
    )


def _record(row: object) -> RunRecord:
    checkpoint_raw = row[5]  # type: ignore[index]
    return RunRecord(
        id=row[0],  # type: ignore[index]
        session_id=row[1],  # type: ignore[index]
        status=row[2],  # type: ignore[index]
        created_at=row[3],  # type: ignore[index]
        updated_at=row[4],  # type: ignore[index]
        checkpoint=(checkpoint_from_dict(json.loads(checkpoint_raw)) if checkpoint_raw else None),
        meta=dict(json.loads(row[6] or "{}")),  # type: ignore[index]
    )


class SqliteRunStore:
    def __init__(self, path: str | Path = ".linch/runs.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._exec = SqliteExecutor(self.path, init=_init_schema, wal=True)

    async def create_run(
        self,
        session_id: str,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> RunRecord:
        return await self._exec.run(lambda c: _create_run(c, session_id, id, meta or {}))

    async def load_run(self, run_id: str) -> RunRecord | None:
        return await self._exec.run(lambda c: _load_run(c, run_id))

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        *,
        status: str = "running",
    ) -> RunRecord:
        return await self._exec.run(lambda c: _save_checkpoint(c, run_id, checkpoint, status))

    async def append_event(self, run_id: str, event: Event) -> int:
        return await self._exec.run(lambda c: _append_event(c, run_id, event))

    async def load_events(self, run_id: str, *, after_seq: int = 0) -> list[StoredRunEvent]:
        return await self._exec.run(lambda c: _load_events(c, run_id, after_seq))

    async def mark_completed(self, run_id: str, checkpoint: RunCheckpoint) -> RunRecord:
        checkpoint.phase = "completed"
        return await self.save_checkpoint(run_id, checkpoint, status="completed")

    async def mark_failed(
        self,
        run_id: str,
        checkpoint: RunCheckpoint | None = None,
        error: dict[str, object] | None = None,
    ) -> RunRecord:
        return await self._exec.run(lambda c: _mark_failed(c, run_id, checkpoint, error))

    async def close(self) -> None:
        await self._exec.close()

    def __enter__(self) -> SqliteRunStore:
        return self

    def __exit__(self, *_: object) -> None:
        self._exec.close_sync()

    async def __aenter__(self) -> SqliteRunStore:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


def _create_run(
    conn: sqlite3.Connection,
    session_id: str,
    id: str | None,
    meta: dict[str, object],
) -> RunRecord:
    rid = id or str(uuid4())
    row = conn.execute(
        "select id, session_id, status, created_at, updated_at, checkpoint, meta "
        "from runs where id = ?",
        (rid,),
    ).fetchone()
    if row:
        return _record(row)
    ts = now_iso()
    conn.execute(
        "insert into runs (id, session_id, status, created_at, updated_at, checkpoint, meta) "
        "values (?, ?, 'running', ?, ?, null, ?)",
        (rid, session_id, ts, ts, json.dumps(meta)),
    )
    conn.commit()
    return RunRecord(
        id=rid,
        session_id=session_id,
        status="running",
        created_at=ts,
        updated_at=ts,
        meta=dict(meta),
    )


def _load_run(conn: sqlite3.Connection, run_id: str) -> RunRecord | None:
    row = conn.execute(
        "select id, session_id, status, created_at, updated_at, checkpoint, meta "
        "from runs where id = ?",
        (run_id,),
    ).fetchone()
    return _record(row) if row else None


def _save_checkpoint(
    conn: sqlite3.Connection,
    run_id: str,
    checkpoint: RunCheckpoint,
    status: str,
) -> RunRecord:
    row = conn.execute(
        "select id, session_id, status, created_at, updated_at, checkpoint, meta "
        "from runs where id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"run not found: {run_id}")
    ts = now_iso()
    conn.execute(
        "update runs set updated_at = ?, status = ?, checkpoint = ? where id = ?",
        (ts, status, json.dumps(checkpoint_to_dict(checkpoint)), run_id),
    )
    conn.commit()
    return RunRecord(
        id=row[0],
        session_id=row[1],
        status=status,  # type: ignore[arg-type]
        created_at=row[3],
        updated_at=ts,
        checkpoint=checkpoint,
        meta=dict(json.loads(row[6] or "{}")),
    )


def _append_event(conn: sqlite3.Connection, run_id: str, event: Event) -> int:
    row = conn.execute("select coalesce(max(seq), 0) from run_events where run_id = ?", (run_id,))
    seq = int(row.fetchone()[0]) + 1
    ts = now_iso()
    conn.execute(
        "insert into run_events (run_id, seq, appended_at, event) values (?, ?, ?, ?)",
        (run_id, seq, ts, json.dumps(event_to_dict(event))),
    )
    conn.commit()
    return seq


def _load_events(
    conn: sqlite3.Connection,
    run_id: str,
    after_seq: int,
) -> list[StoredRunEvent]:
    rows = conn.execute(
        "select seq, appended_at, event from run_events where run_id = ? and seq > ? order by seq",
        (run_id, after_seq),
    ).fetchall()
    return [
        StoredRunEvent(
            seq=row[0],
            appended_at=row[1],
            event=event_from_dict(json.loads(row[2])),
        )
        for row in rows
    ]


def _mark_failed(
    conn: sqlite3.Connection,
    run_id: str,
    checkpoint: RunCheckpoint | None,
    error: dict[str, object] | None,
) -> RunRecord:
    row = conn.execute(
        "select id, session_id, status, created_at, updated_at, checkpoint, meta "
        "from runs where id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"run not found: {run_id}")
    meta = dict(json.loads(row[6] or "{}"))
    if error is not None:
        errors = meta.setdefault("errors", [])
        if isinstance(errors, list):
            errors.append(error)
    ts = now_iso()
    checkpoint_json = row[5]
    if checkpoint is not None:
        checkpoint.phase = "failed"
        checkpoint_json = json.dumps(checkpoint_to_dict(checkpoint))
    conn.execute(
        "update runs set updated_at = ?, status = 'failed', checkpoint = ?, meta = ? where id = ?",
        (ts, checkpoint_json, json.dumps(meta), run_id),
    )
    conn.commit()
    return RunRecord(
        id=row[0],
        session_id=row[1],
        status="failed",
        created_at=row[3],
        updated_at=ts,
        checkpoint=checkpoint,
        meta=meta,
    )

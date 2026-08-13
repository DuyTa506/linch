"""Portable copy-on-fork support built on the public session-store protocol.

The helper deliberately copies only durable conversation history and the skill
records that belong to that history.  Everything else is initialized by
``Agent.session`` as fresh per-session runtime state.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, cast

from linch.errors import ConfigError
from linch.types import (
    Message,
    ToolResultBlock,
    ToolUseBlock,
    message_from_dict,
    message_to_dict,
)

from .store import SessionRecord, SessionStore, StoredMessage

if TYPE_CHECKING:
    from linch.agent import Agent
    from linch.session import Session


FORKED_FROM_SESSION_ID = "forked_from_session_id"
FORKED_AT_SEQ = "forked_at_seq"
_logger = logging.getLogger(__name__)


async def fork_session(
    agent: Agent,
    source_or_id: Session | str,
    *,
    before_seq: int | None = None,
    id: str | None = None,
    meta: Mapping[str, object] | None = None,
) -> Session:
    """Create an independent session from a durable source-history prefix.

    ``before_seq`` is exclusive: the stored message with that sequence number
    and every later message are omitted.  With no boundary, the complete
    durable tail is copied.  An active live source therefore requires an
    explicit boundary so concurrently appended tail messages cannot make the
    requested fork point ambiguous.

    The destination receives copied messages, applicable invoked-skill records,
    source metadata plus caller overrides, and two provenance keys:
    ``forked_from_session_id`` and ``forked_at_seq`` (the highest copied source
    sequence, or ``0`` for an empty prefix).  It does not inherit provider-view
    snapshots, run checkpoints, tasks, approvals, workers, filesystem state, or
    other process-local session state.
    """

    from linch.session import Session

    if before_seq is not None:
        if isinstance(before_seq, bool) or not isinstance(before_seq, int) or before_seq < 1:
            raise ConfigError("before_seq must be a positive integer")
    if id is not None and (not isinstance(id, str) or not id):
        raise ConfigError("fork session id must be a non-empty string")
    if meta is not None and not isinstance(meta, Mapping):
        raise ConfigError("fork session meta must be a mapping")

    store: SessionStore
    if isinstance(source_or_id, Session):
        if source_or_id.agent is not agent:
            raise ConfigError("source session belongs to a different agent")
        source_id = source_or_id.id
        store = source_or_id.store
    elif isinstance(source_or_id, str) and source_or_id:
        source_id = source_or_id
        store = agent._get_store()
    else:
        raise ConfigError("source session must be a Session or non-empty session id")

    if id == source_id:
        raise ConfigError("fork destination id must differ from the source id")

    live_source = (
        source_or_id if isinstance(source_or_id, Session) else agent._sessions.get(source_id)
    )
    if live_source is not None and live_source._active and before_seq is None:
        raise ConfigError("an active source session requires an explicit before_seq boundary")

    source_record = await store.load(source_id)
    if source_record is None:
        raise ConfigError(f"source session not found: {source_id}")
    source_rows = await store.load_messages(source_id)
    _validate_stored_sequences(source_rows)
    copied_rows = _select_prefix(source_rows, before_seq)
    _validate_conversation_boundary(copied_rows)

    fork_at_seq = copied_rows[-1].seq if copied_rows else 0
    fork_meta = deepcopy(source_record.meta)
    if meta:
        fork_meta.update(deepcopy(dict(meta)))
    # Lineage is controlled by the fork operation, not caller-supplied metadata.
    fork_meta[FORKED_FROM_SESSION_ID] = source_id
    fork_meta[FORKED_AT_SEQ] = fork_at_seq

    messages = [_copy_message(row.message) for row in copied_rows]
    skills = _skills_for_prefix(source_record, copied_rows, before_seq)

    target_record: SessionRecord | None = None
    owns_target = False
    try:
        if id is None:
            # The required SessionStore contract defines create-without-id as a
            # fresh session allocation.
            target_record = await store.create(meta=fork_meta)
        else:
            # Required because SessionStore.create(id=...) is intentionally
            # idempotent and cannot distinguish a newly created target from a
            # concurrently-created one.  Never append to a target we do not
            # positively own.
            create_if_absent = getattr(store, "create_if_absent", None)
            if create_if_absent is None:
                raise ConfigError(
                    "this session store does not support atomic custom ids for forks; omit id"
                )
            target_record = await create_if_absent(id=id, meta=fork_meta)
            if target_record is None:
                raise ConfigError(f"fork destination session already exists: {id}")
        owns_target = True
        if messages:
            await store.append_messages(target_record.id, messages)
        if skills:
            await store.set_invoked_skills(target_record.id, skills)

        target = await agent.session(id=target_record.id)
        if target._active:
            raise RuntimeError("newly forked session unexpectedly became active")
        return target
    except BaseException:
        if owns_target and target_record is not None and target_record.id != source_id:
            registered = agent._sessions.get(target_record.id)
            # A concurrent ``Agent.session(id=...)`` may have attached to the
            # durable fork between creation and handoff.  Never evict or delete
            # storage underneath another live (especially active) Session.
            if registered is None or registered._closed:
                if registered is not None:
                    agent._sessions.pop(target_record.id, None)
                try:
                    await store.delete(target_record.id)
                except Exception:
                    _logger.warning(
                        "failed to clean up partially forked session %s",
                        target_record.id,
                        exc_info=True,
                    )
            else:
                _logger.warning(
                    "skipping cleanup of partially forked session %s because it is live",
                    target_record.id,
                )
        raise


def _select_prefix(rows: list[StoredMessage], before_seq: int | None) -> list[StoredMessage]:
    if before_seq is None:
        return list(rows)
    if not any(row.seq == before_seq for row in rows):
        raise ConfigError(f"before_seq does not identify a stored source message: {before_seq}")
    return [row for row in rows if row.seq < before_seq]


def _validate_stored_sequences(rows: list[StoredMessage]) -> None:
    previous = 0
    for row in rows:
        if isinstance(row.seq, bool) or not isinstance(row.seq, int) or row.seq <= previous:
            raise ConfigError(
                "source session message sequences must be strictly increasing integers"
            )
        previous = row.seq


def _validate_conversation_boundary(rows: list[StoredMessage]) -> None:
    """Reject a prefix that ends inside a provider tool-use exchange."""

    pending: dict[str, str] = {}
    for row in rows:
        message = row.message
        tool_uses = [block for block in message.content if isinstance(block, ToolUseBlock)]
        tool_results = [block for block in message.content if isinstance(block, ToolResultBlock)]

        if message.role == "assistant" and tool_uses:
            if pending:
                raise ConfigError(
                    "unsafe fork boundary: assistant tool calls before "
                    f"seq {row.seq} are unanswered"
                )
            ids = [block.id for block in tool_uses]
            if len(ids) != len(set(ids)) or any(not tool_id for tool_id in ids):
                raise ConfigError(f"source session has invalid tool-use ids at seq {row.seq}")
            pending = {block.id: block.name for block in tool_uses}
            continue

        if pending:
            result_ids = {block.tool_use_id for block in tool_results}
            if message.role != "user" or result_ids != set(pending):
                raise ConfigError(
                    f"source session has an unpaired tool-use exchange at seq {row.seq}"
                )
            pending.clear()

    if pending:
        ids = ", ".join(sorted(pending))
        raise ConfigError(f"unsafe fork boundary leaves assistant tool calls unanswered: {ids}")


def _skills_for_prefix(
    source: SessionRecord,
    rows: list[StoredMessage],
    before_seq: int | None,
) -> list[dict[str, object]]:
    # A tail fork preserves future-compatible records byte-for-shape.  For an
    # earlier prefix, retain one record for each successful Skill tool call in
    # that prefix; this prevents instructions invoked later on the source branch
    # from leaking into the fork's system prompt.
    if before_seq is None:
        return deepcopy(source.invoked_skills)

    skill_uses: dict[str, str] = {}
    successful_ids: set[str] = set()
    for row in rows:
        for block in row.message.content:
            if isinstance(block, ToolUseBlock) and block.name.casefold() == "skill":
                requested = block.input.get("skill")
                if isinstance(requested, str) and requested.strip():
                    skill_uses[block.id] = requested.strip().removeprefix("/")
            elif isinstance(block, ToolResultBlock) and not block.is_error:
                successful_ids.add(block.tool_use_id)

    wanted = Counter(skill_uses[tool_id] for tool_id in successful_ids if tool_id in skill_uses)
    selected: list[dict[str, object]] = []
    for raw in source.invoked_skills:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", ""))
        if wanted[name] <= 0:
            continue
        selected.append(deepcopy(cast(dict[str, object], raw)))
        wanted[name] -= 1
    return selected


def _copy_message(message: Message) -> Message:
    # InMemorySessionStore otherwise shares mutable Message/Block instances
    # between source and child.  The wire round-trip also matches durable-store
    # behavior and avoids relying on implementation internals.
    return message_from_dict(deepcopy(message_to_dict(message)))


__all__ = ["fork_session"]

"""Durable :class:`Mailbox` backed by SQLite.

All SQLite work runs through :class:`~linch.storage._executor.SqliteExecutor`,
so the event loop is not blocked. ``drain`` is destructive and transactional:
one recipient's pending messages are selected and deleted under one immediate
write transaction.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ...storage._executor import SqliteExecutor
from .core import MailboxMessage


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mailbox_messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            content TEXT NOT NULL,
            type TEXT NOT NULL,
            request_id TEXT,
            in_reply_to TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mailbox_messages_recipient_seq
        ON mailbox_messages(recipient, seq)
        """
    )


class SqliteMailbox:
    """SQLite-backed mailbox for cross-process peer message delivery."""

    def __init__(self, path: str | Path) -> None:
        # No default: the class promises durable cross-process delivery, so the
        # path is required. Pass ":memory:" explicitly for an ephemeral,
        # single-process mailbox (e.g. in tests).
        self.path = str(path)
        self._exec = SqliteExecutor(self.path, init=_init_schema, wal=True)

    async def send(self, message: MailboxMessage) -> None:
        await self._exec.run(lambda conn: _insert(conn, message))

    async def drain(self, recipient: str) -> list[MailboxMessage]:
        return await self._exec.run(lambda conn: _drain(conn, recipient))

    async def aclose(self) -> None:
        await self._exec.close()

    def close(self) -> None:
        self._exec.close_sync()

    def __enter__(self) -> SqliteMailbox:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    async def __aenter__(self) -> SqliteMailbox:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def _insert(conn: sqlite3.Connection, message: MailboxMessage) -> None:
    conn.execute(
        """
        INSERT INTO mailbox_messages(
            id, sender, recipient, content, type, request_id, in_reply_to
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message.id,
            message.sender,
            message.recipient,
            message.content,
            message.type,
            message.request_id,
            message.in_reply_to,
        ),
    )
    conn.commit()


def _drain(conn: sqlite3.Connection, recipient: str) -> list[MailboxMessage]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = _fetch_recipient(conn, recipient)
        if rows:
            # The whole drain holds the write lock (BEGIN IMMEDIATE) and inserts
            # nothing, so no row can appear between the SELECT and this DELETE —
            # a single recipient-scoped DELETE removes exactly the fetched rows
            # without N per-seq statements.
            conn.execute("DELETE FROM mailbox_messages WHERE recipient = ?", (recipient,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return [_row_to_message(row) for row in rows]


def _fetch_recipient(conn: sqlite3.Connection, recipient: str) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT seq, id, sender, recipient, content, type, request_id, in_reply_to
        FROM mailbox_messages
        WHERE recipient = ?
        ORDER BY seq ASC
        """,
        (recipient,),
    )
    return [dict(row) for row in cursor.fetchall()]


def _row_to_message(row: dict[str, Any]) -> MailboxMessage:
    return MailboxMessage(
        id=str(row["id"]),
        sender=str(row["sender"]),
        recipient=str(row["recipient"]),
        content=str(row["content"]),
        type=str(row["type"]),
        request_id=_nullable_str(row["request_id"]),
        in_reply_to=_nullable_str(row["in_reply_to"]),
    )


def _nullable_str(value: object) -> str | None:
    return value if isinstance(value, str) else None

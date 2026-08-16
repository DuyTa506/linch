"""Durable :class:`Mailbox` backed by SQLite.

All SQLite work runs through :class:`~linch.storage._executor.SqliteExecutor`,
so the event loop is not blocked. ``drain`` is destructive and transactional:
one recipient's pending messages are selected and deleted under one immediate
write transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...storage._executor import SqliteExecutor
from .core import MailboxClaim, MailboxMessage, _validate_claim_args


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
    # Kept separate from the legacy message table so opening a 2.0 database is
    # an additive table/index migration and never rewrites message history.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mailbox_claims (
            message_id TEXT PRIMARY KEY,
            token TEXT NOT NULL UNIQUE,
            owner TEXT NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mailbox_claims_expiry
        ON mailbox_claims(expires_at)
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

    async def claim(
        self,
        recipient: str,
        *,
        owner: str,
        lease_s: float,
        now: float | None = None,
        limit: int | None = None,
    ) -> list[MailboxClaim]:
        import time

        claimed_at = time.time() if now is None else now
        _validate_claim_args(owner=owner, lease_s=lease_s, now=claimed_at, limit=limit)
        if limit == 0:
            return []
        return await self._exec.run(
            lambda conn: _claim(
                conn,
                recipient,
                owner=owner,
                lease_s=lease_s,
                now=claimed_at,
                limit=limit,
            )
        )

    async def ack(self, claims: Sequence[MailboxClaim]) -> None:
        if claims:
            await self._exec.run(lambda conn: _ack(conn, claims))

    async def release(self, claims: Sequence[MailboxClaim]) -> None:
        if claims:
            await self._exec.run(lambda conn: _release(conn, claims))

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
            conn.executemany(
                "DELETE FROM mailbox_claims WHERE message_id = ?",
                [(str(row["id"]),) for row in rows],
            )
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


def _claim(
    conn: sqlite3.Connection,
    recipient: str,
    *,
    owner: str,
    lease_s: float,
    now: float,
    limit: int | None,
) -> list[MailboxClaim]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = _fetch_recipient_with_claim(conn, recipient)
        claimed: list[MailboxClaim] = []
        for row in rows:
            token_value = row["lease_token"]
            expires_value = row["lease_expires_at"]
            # Do not skip an older message protected by any valid lease.  This
            # is what makes delivery FIFO even with multiple consumers.
            if token_value is not None and expires_value is not None and float(expires_value) > now:
                break

            token = uuid4().hex
            expires_at = now + lease_s
            conn.execute(
                """
                INSERT INTO mailbox_claims(message_id, token, owner, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    token=excluded.token,
                    owner=excluded.owner,
                    expires_at=excluded.expires_at
                """,
                (str(row["id"]), token, owner, expires_at),
            )
            claimed.append(
                MailboxClaim(
                    message=_row_to_message(row),
                    token=token,
                    owner=owner,
                    expires_at=expires_at,
                )
            )
            if limit is not None and len(claimed) >= limit:
                break
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return claimed


def _fetch_recipient_with_claim(conn: sqlite3.Connection, recipient: str) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT
            m.seq, m.id, m.sender, m.recipient, m.content, m.type,
            m.request_id, m.in_reply_to,
            c.token AS lease_token, c.expires_at AS lease_expires_at
        FROM mailbox_messages AS m
        LEFT JOIN mailbox_claims AS c ON c.message_id = m.id
        WHERE m.recipient = ?
        ORDER BY m.seq ASC
        """,
        (recipient,),
    )
    return [dict(row) for row in cursor.fetchall()]


def _ack(conn: sqlite3.Connection, claims: Sequence[MailboxClaim]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for claim in claims:
            # Delete only through the live capability.  A late ack from an
            # expired claimant cannot delete a row reclaimed by another owner.
            cursor = conn.execute(
                "DELETE FROM mailbox_claims WHERE message_id = ? AND token = ?",
                (claim.message.id, claim.token),
            )
            if cursor.rowcount:
                conn.execute("DELETE FROM mailbox_messages WHERE id = ?", (claim.message.id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _release(conn: sqlite3.Connection, claims: Sequence[MailboxClaim]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "DELETE FROM mailbox_claims WHERE message_id = ? AND token = ?",
            [(claim.message.id, claim.token) for claim in claims],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


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

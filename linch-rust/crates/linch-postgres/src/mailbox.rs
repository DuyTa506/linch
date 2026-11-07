//! [`PgMailbox`]: peer messages in Postgres, leased to the drain that
//! takes them.
//!
//! Each call is one statement in autocommit. A drain locks the waiting
//! rows it can (`FOR UPDATE SKIP LOCKED`) and leases them in the same
//! statement, so concurrent drains, from any process, never return one
//! message twice. Lease times are the server's clock, so processes with
//! different clocks agree on when a lease ran out.

use std::time::Duration;

use async_trait::async_trait;
use linch_core::{Mailbox, MailboxError, MailboxMessage};
use sqlx::postgres::PgRow;
use sqlx::{PgPool, Row};

/// How long a drain holds what it returns before another drain may take
/// it again, unless [`PgMailbox::with_lease`] says otherwise. A run acks
/// its messages within moments of the drain; the lease only bounds how
/// long a message a dead process drained waits to come again.
pub const DEFAULT_LEASE: Duration = Duration::from_secs(60);

/// Lease every waiting message of one inbox that no live lease holds,
/// returning them with their send order.
const DRAIN: &str = "WITH picked AS ( \
         SELECT id FROM linch_mailbox \
         WHERE namespace = $1 AND recipient = $2 \
           AND (lease_until IS NULL OR lease_until <= now()) \
         ORDER BY seq FOR UPDATE SKIP LOCKED \
     ) UPDATE linch_mailbox m \
     SET lease_token = $3, lease_until = now() + make_interval(secs => $4) \
     FROM picked WHERE m.namespace = $1 AND m.recipient = $2 AND m.id = picked.id \
     RETURNING m.seq, m.sender, m.recipient, m.content, m.kind, m.request_id, \
         m.in_reply_to, m.id";

/// The rows of `($3 recipients, $4 ids)` this mailbox's token `$2` holds.
const HELD: &str = "namespace = $1 AND lease_token = $2 \
     AND (recipient, id) IN (SELECT * FROM unnest($3::text[], $4::text[]))";

/// A [`Mailbox`] backed by Postgres, for the agents of one namespace.
///
/// Inboxes are keyed by `(namespace, recipient)`, so two tenants with the
/// same session ids never see each other's messages. A message stays in
/// its row until a drain's ack deletes it; a drain that is never acked or
/// released (its process died) holds it for the lease, then the next
/// drain takes it again. Delivery is at least once. Run [`crate::migrate`]
/// on the database before the first call.
#[derive(Debug, Clone)]
pub struct PgMailbox {
    pool: PgPool,
    namespace: String,
    /// Names the leases this mailbox holds, so an ack or release never
    /// touches a lease another mailbox took after this one's ran out.
    token: String,
    lease: Duration,
}

impl PgMailbox {
    /// A mailbox for `namespace` that runs its queries on `pool`, with the
    /// [`DEFAULT_LEASE`].
    pub fn new(pool: PgPool, namespace: impl Into<String>) -> Self {
        Self {
            pool,
            namespace: namespace.into(),
            token: uuid::Uuid::new_v4().simple().to_string(),
            lease: DEFAULT_LEASE,
        }
    }

    /// This mailbox with its drains holding messages for `lease`.
    pub fn with_lease(mut self, lease: Duration) -> Self {
        self.lease = lease;
        self
    }

    /// Run `UPDATE`/`DELETE` statement `head` on the rows of `messages`
    /// this mailbox holds.
    async fn settle(&self, head: &str, messages: &[MailboxMessage]) -> Result<(), MailboxError> {
        if messages.is_empty() {
            return Ok(());
        }
        let recipients: Vec<&str> = messages.iter().map(|m| m.recipient.as_str()).collect();
        let ids: Vec<&str> = messages.iter().map(|m| m.id.as_str()).collect();
        let sql = format!("{head} WHERE {HELD}");
        sqlx::query(sqlx::AssertSqlSafe(sql))
            .bind(&self.namespace)
            .bind(&self.token)
            .bind(recipients)
            .bind(ids)
            .execute(&self.pool)
            .await
            .map_err(backend)?;
        Ok(())
    }
}

fn backend(error: sqlx::Error) -> MailboxError {
    MailboxError(Box::new(error))
}

fn message(row: &PgRow) -> Result<(i64, MailboxMessage), sqlx::Error> {
    let message = MailboxMessage {
        sender: row.try_get("sender")?,
        recipient: row.try_get("recipient")?,
        content: row.try_get("content")?,
        kind: row.try_get("kind")?,
        request_id: row.try_get("request_id")?,
        in_reply_to: row.try_get("in_reply_to")?,
        id: row.try_get("id")?,
    };
    Ok((row.try_get("seq")?, message))
}

#[async_trait]
impl Mailbox for PgMailbox {
    async fn send(&self, message: MailboxMessage) -> Result<(), MailboxError> {
        // A message already queued under its id stays as it is.
        sqlx::query(
            "INSERT INTO linch_mailbox \
             (namespace, recipient, id, sender, content, kind, request_id, in_reply_to) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING",
        )
        .bind(&self.namespace)
        .bind(&message.recipient)
        .bind(&message.id)
        .bind(&message.sender)
        .bind(&message.content)
        .bind(&message.kind)
        .bind(&message.request_id)
        .bind(&message.in_reply_to)
        .execute(&self.pool)
        .await
        .map_err(backend)?;
        Ok(())
    }

    async fn drain(&self, recipient: &str) -> Result<Vec<MailboxMessage>, MailboxError> {
        let rows = sqlx::query(DRAIN)
            .bind(&self.namespace)
            .bind(recipient)
            .bind(&self.token)
            .bind(self.lease.as_secs_f64())
            .fetch_all(&self.pool)
            .await
            .map_err(backend)?;
        let decoded = rows.iter().map(message).collect::<Result<Vec<_>, _>>();
        let mut drained = decoded.map_err(backend)?;
        // RETURNING keeps no order; the send order is the seq.
        drained.sort_unstable_by_key(|(seq, _)| *seq);
        Ok(drained.into_iter().map(|(_, message)| message).collect())
    }

    async fn ack(&self, messages: &[MailboxMessage]) -> Result<(), MailboxError> {
        self.settle("DELETE FROM linch_mailbox", messages).await
    }

    async fn release(&self, messages: &[MailboxMessage]) -> Result<(), MailboxError> {
        let head = "UPDATE linch_mailbox SET lease_token = NULL, lease_until = NULL";
        self.settle(head, messages).await
    }
}

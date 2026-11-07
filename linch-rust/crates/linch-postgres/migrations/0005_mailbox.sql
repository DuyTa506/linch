-- Peer messages (linch_core::Mailbox on PgMailbox). An inbox is keyed by
-- namespace and recipient (a session id); `seq` keeps the order of sends.
-- A drain leases a message: `lease_token` names the mailbox that holds it
-- until `lease_until`, after which any drain may take it again. An ack
-- deletes the row. There is no key to linch_sessions: a message may wait
-- for a session that has no run yet.

CREATE TABLE linch_mailbox (
    namespace   TEXT   NOT NULL,
    recipient   TEXT   NOT NULL,
    id          TEXT   NOT NULL,
    seq         BIGINT GENERATED ALWAYS AS IDENTITY,
    sender      TEXT   NOT NULL,
    content     TEXT   NOT NULL,
    kind        TEXT   NOT NULL,
    request_id  TEXT,
    in_reply_to TEXT,
    lease_token TEXT,
    lease_until TIMESTAMPTZ,
    PRIMARY KEY (namespace, recipient, id),
    CHECK ((lease_token IS NULL) = (lease_until IS NULL))
);

CREATE INDEX linch_mailbox_order ON linch_mailbox (namespace, recipient, seq);

-- Run and session stores. JSON payloads are TEXT, not JSONB: JSONB reorders
-- keys and rewrites numbers, and the stores must hand back exactly what was
-- written (message bytes feed provider prompt caches and digests).

CREATE TABLE linch_runs (
    namespace     TEXT   NOT NULL,
    run_id        TEXT   NOT NULL,
    session_id    TEXT   NOT NULL,
    parent_run_id TEXT,
    contract      TEXT   NOT NULL,
    status        TEXT   NOT NULL CHECK (status IN (
        'pending', 'running', 'awaiting_approval', 'awaiting_input',
        'completed', 'failed', 'aborted'
    )),
    epoch         BIGINT NOT NULL CHECK (epoch >= 0),
    owner         TEXT,
    checkpoint    TEXT   NOT NULL,
    last_seq      BIGINT NOT NULL CHECK (last_seq >= 0),
    PRIMARY KEY (namespace, run_id)
);

CREATE TABLE linch_run_events (
    namespace TEXT   NOT NULL,
    run_id    TEXT   NOT NULL,
    seq       BIGINT NOT NULL CHECK (seq >= 1),
    envelope  TEXT   NOT NULL,
    PRIMARY KEY (namespace, run_id, seq),
    FOREIGN KEY (namespace, run_id) REFERENCES linch_runs (namespace, run_id)
);

CREATE TABLE linch_sessions (
    namespace  TEXT   NOT NULL,
    session_id TEXT   NOT NULL,
    len        BIGINT NOT NULL CHECK (len >= 0),
    PRIMARY KEY (namespace, session_id)
);

CREATE TABLE linch_session_entries (
    namespace  TEXT   NOT NULL,
    session_id TEXT   NOT NULL,
    position   BIGINT NOT NULL CHECK (position >= 0),
    entry      TEXT   NOT NULL,
    PRIMARY KEY (namespace, session_id, position),
    FOREIGN KEY (namespace, session_id) REFERENCES linch_sessions (namespace, session_id)
);

-- Session tasks (linch_core::TaskStore on PgSessionStore). A task id is a
-- number handed out per session from next_task_id and never reused. Every
-- task write locks the session row first, so the task writes of a session
-- serialize. An edge row says `blocker` blocks `blocked`; either end may
-- name a task that does not exist (yet), and `position` keeps the order in
-- which edges were added.

ALTER TABLE linch_sessions
    ADD COLUMN next_task_id BIGINT NOT NULL DEFAULT 1 CHECK (next_task_id >= 1);

CREATE TABLE linch_tasks (
    namespace     TEXT   NOT NULL,
    session_id    TEXT   NOT NULL,
    task_id       BIGINT NOT NULL CHECK (task_id >= 1),
    subject       TEXT   NOT NULL,
    description   TEXT   NOT NULL,
    active_form   TEXT,
    status        TEXT   NOT NULL CHECK (status IN ('pending', 'in_progress', 'completed')),
    owner         TEXT,
    metadata      TEXT   NOT NULL,
    created_at_ms BIGINT NOT NULL CHECK (created_at_ms >= 0),
    updated_at_ms BIGINT NOT NULL CHECK (updated_at_ms >= 0),
    PRIMARY KEY (namespace, session_id, task_id),
    FOREIGN KEY (namespace, session_id) REFERENCES linch_sessions (namespace, session_id)
);

CREATE TABLE linch_task_edges (
    namespace  TEXT   NOT NULL,
    session_id TEXT   NOT NULL,
    blocker    TEXT   NOT NULL,
    blocked    TEXT   NOT NULL,
    position   BIGINT GENERATED ALWAYS AS IDENTITY,
    PRIMARY KEY (namespace, session_id, blocker, blocked),
    FOREIGN KEY (namespace, session_id) REFERENCES linch_sessions (namespace, session_id)
);

CREATE INDEX linch_task_edges_blocked ON linch_task_edges (namespace, session_id, blocked);

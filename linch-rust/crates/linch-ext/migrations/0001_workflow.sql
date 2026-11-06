-- Workflow journal. JSON is TEXT, not JSONB: JSONB reorders keys and
-- rewrites numbers, and a replayed value must read back exactly as written.

-- One row per workflow. epoch is the lease: each claim adds 1, and every
-- write names the epoch it holds, so a fenced holder changes nothing.
-- interrupt is the pending question of a suspended workflow, as JSON.
CREATE TABLE linch_ext_workflows (
    namespace   TEXT   NOT NULL,
    workflow_id TEXT   NOT NULL,
    status      TEXT   NOT NULL CHECK (status IN (
        'running', 'awaiting_input', 'completed', 'failed'
    )),
    epoch       BIGINT NOT NULL CHECK (epoch >= 1),
    interrupt   TEXT,
    PRIMARY KEY (namespace, workflow_id),
    CHECK ((status = 'awaiting_input') = (interrupt IS NOT NULL))
);

-- One row per finished call: its content key, its occurrence and its
-- result.
CREATE TABLE linch_ext_workflow_records (
    namespace   TEXT   NOT NULL,
    workflow_id TEXT   NOT NULL,
    call_key    TEXT   NOT NULL,
    occurrence  BIGINT NOT NULL CHECK (occurrence >= 0),
    value       TEXT   NOT NULL,
    PRIMARY KEY (namespace, workflow_id, call_key, occurrence),
    FOREIGN KEY (namespace, workflow_id)
        REFERENCES linch_ext_workflows (namespace, workflow_id)
);

-- Worker queue and cron schedules. Every time is the database clock
-- (now()), never a worker's clock.

-- One row per queued unit of work. A job starts a run (session_id and
-- prompt), continues one (run_id), or approves one (run_id and approvals).
-- A start job records the run id it created before the run's first write,
-- so a retry continues that run instead of starting another.
--
-- visible_at is the visibility timeout: a dequeue moves it forward by the
-- timeout and bumps attempts, which is the fencing token for the heartbeat,
-- ack and release. A job whose owner stopped heartbeating becomes visible
-- again. A done job is deleted; a failed one keeps its error.
CREATE TABLE linch_worker_jobs (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    queue      TEXT        NOT NULL,
    namespace  TEXT        NOT NULL,
    session_id TEXT,
    prompt     TEXT,
    run_id     TEXT,
    approvals  TEXT,
    dedupe_key TEXT,
    visible_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts   INTEGER     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    owner      TEXT,
    error      TEXT,
    failed_at  TIMESTAMPTZ,
    UNIQUE (namespace, dedupe_key),
    CHECK ((session_id IS NULL) = (prompt IS NULL)),
    CHECK (run_id IS NOT NULL OR prompt IS NOT NULL),
    CHECK (approvals IS NULL OR (run_id IS NOT NULL AND prompt IS NULL))
);

CREATE INDEX linch_worker_jobs_ready ON linch_worker_jobs (queue, visible_at, id)
    WHERE failed_at IS NULL;

-- A cron schedule (UTC) that enqueues a start job per fire. next_fire only
-- moves forward, in the same transaction that enqueues the fire's job.
CREATE TABLE linch_worker_schedules (
    namespace  TEXT        NOT NULL,
    id         TEXT        NOT NULL,
    queue      TEXT        NOT NULL,
    session_id TEXT        NOT NULL,
    prompt     TEXT        NOT NULL,
    cron       TEXT        NOT NULL,
    enabled    BOOLEAN     NOT NULL DEFAULT TRUE,
    next_fire  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (namespace, id)
);

CREATE INDEX linch_worker_schedules_due ON linch_worker_schedules (next_fire)
    WHERE enabled;

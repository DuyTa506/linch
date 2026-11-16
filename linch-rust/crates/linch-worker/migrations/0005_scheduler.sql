-- The scheduler: stored agent definitions, interval and time-zone
-- schedules, schedules whose fires each start a new session, and the
-- history of finished jobs.

-- An agent definition (a serialized agent config, without its store DSN)
-- that a job or a schedule runs with, keyed by the digest of its text: a
-- definition never changes once stored, and a job or schedule that names
-- one always runs the config it was created with. Rows are never deleted
-- by the queue; they are small and shared by every job of a schedule.
CREATE TABLE linch_worker_agents (
    namespace TEXT NOT NULL,
    digest    TEXT NOT NULL,
    config    TEXT NOT NULL,
    PRIMARY KEY (namespace, digest)
);

-- A schedule fires on a cron pattern in its time zone, or every
-- interval_ms. Without a session_id each fire starts a new session; with
-- one, every fire is a new run in that session, and a fire is skipped
-- while an earlier fire's job is unfinished or the session has a live run.
-- fires and skips count what each fire did; last_fire is the latest.
ALTER TABLE linch_worker_schedules
    ALTER COLUMN cron DROP NOT NULL,
    ALTER COLUMN session_id DROP NOT NULL,
    ADD COLUMN interval_ms BIGINT CHECK (interval_ms > 0),
    ADD COLUMN timezone    TEXT        NOT NULL DEFAULT 'UTC',
    ADD COLUMN agent       TEXT,
    ADD COLUMN created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN last_fire   TIMESTAMPTZ,
    ADD COLUMN fires       BIGINT      NOT NULL DEFAULT 0 CHECK (fires >= 0),
    ADD COLUMN skips       BIGINT      NOT NULL DEFAULT 0 CHECK (skips >= 0),
    ADD CONSTRAINT linch_worker_schedules_timing CHECK ((cron IS NULL) <> (interval_ms IS NULL)),
    ADD CONSTRAINT linch_worker_schedules_agent FOREIGN KEY (namespace, agent)
        REFERENCES linch_worker_agents (namespace, digest);

-- A job names the agent definition it runs with (NULL: the worker's own
-- agent for the namespace), and the schedule and fire time that made it.
ALTER TABLE linch_worker_jobs
    ADD COLUMN agent       TEXT,
    ADD COLUMN schedule_id TEXT,
    ADD COLUMN fire_at     TIMESTAMPTZ,
    ADD COLUMN created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD CONSTRAINT linch_worker_jobs_agent FOREIGN KEY (namespace, agent)
        REFERENCES linch_worker_agents (namespace, digest);

CREATE INDEX linch_worker_jobs_schedule ON linch_worker_jobs (namespace, schedule_id)
    WHERE schedule_id IS NOT NULL;

-- A job that ended: written by the same statement that deletes the job,
-- so a job is either in linch_worker_jobs or here, never both or neither.
-- outcome is 'done' (its run ended or suspended) or 'superseded' (another
-- owner claimed its run, which goes on there). Failed jobs stay in
-- linch_worker_jobs with failed_at set.
CREATE TABLE linch_worker_history (
    id          BIGINT      PRIMARY KEY,
    queue       TEXT        NOT NULL,
    namespace   TEXT        NOT NULL,
    session_id  TEXT,
    run_id      TEXT,
    schedule_id TEXT,
    fire_at     TIMESTAMPTZ,
    attempts    INTEGER     NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome     TEXT        NOT NULL CHECK (outcome IN ('done', 'superseded'))
);

CREATE INDEX linch_worker_history_namespace ON linch_worker_history (namespace, id);
CREATE INDEX linch_worker_history_finished ON linch_worker_history (finished_at);

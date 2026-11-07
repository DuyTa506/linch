-- At most one active job per run: two jobs on one run would claim it from
-- each other. A failed job keeps its row but no longer counts. A start job
-- names its run only once it records it, so it never conflicts before.
CREATE UNIQUE INDEX linch_worker_jobs_one_per_run ON linch_worker_jobs (namespace, run_id)
    WHERE failed_at IS NULL;

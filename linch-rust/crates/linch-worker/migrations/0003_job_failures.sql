-- Failed attempts of a job: attempts that released it for a retry after an
-- error. A wait for a busy session is not one, nor is a takeover, so this
-- is separate from attempts (the dequeue count and fencing token). A job
-- is failed for good once it reaches the worker's cap.
ALTER TABLE linch_worker_jobs
    ADD COLUMN failures INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0);

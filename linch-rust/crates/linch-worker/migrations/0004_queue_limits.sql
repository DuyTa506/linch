-- A queue's concurrency limit, kept with the queue so every worker that
-- serves it enforces the same one: at most max_held of its jobs are held
-- at once across the fleet. A queue without a row, or with 0, has none.
CREATE TABLE linch_worker_queues (
    name     TEXT    PRIMARY KEY,
    max_held INTEGER NOT NULL CHECK (max_held >= 0)
);

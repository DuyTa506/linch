-- Admission (RunStore::create): a session holds at most one run that is not
-- terminal, per namespace. The status list is RunStatus::is_terminal; a
-- unit test in src/run.rs keeps the two in step.
CREATE UNIQUE INDEX linch_runs_one_live_per_session ON linch_runs (namespace, session_id)
    WHERE status NOT IN ('completed', 'failed', 'aborted');

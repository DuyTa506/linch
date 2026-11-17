-- The agent definition an ended job ran with, so a continuation of its run
-- (enqueue_resume, enqueue_approve) runs with the same one: a run that
-- suspended for an approval has only history rows. No foreign key: the
-- definition may be pruned once its run is terminal, and the history row
-- stays. While the run is not terminal, prune_definitions keeps it.
ALTER TABLE linch_worker_history ADD COLUMN agent TEXT;

CREATE INDEX linch_worker_history_run ON linch_worker_history (namespace, run_id)
    WHERE run_id IS NOT NULL;
CREATE INDEX linch_worker_history_agent ON linch_worker_history (namespace, agent)
    WHERE agent IS NOT NULL;

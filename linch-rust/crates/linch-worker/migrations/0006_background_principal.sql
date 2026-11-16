-- Who a background run acts for, and the lookups that prune stored agent
-- definitions.

-- The principal (linch_core::Principal as JSON) every fire of a schedule
-- and the run of a job act for, so a permission rule that matches on a
-- role holds in the background as it does for a run started directly.
-- A fire copies its schedule's principal to its job. NULL: no principal.
ALTER TABLE linch_worker_schedules ADD COLUMN principal TEXT;
ALTER TABLE linch_worker_jobs ADD COLUMN principal TEXT;

-- A stored definition holds config secrets (MCP env and header values),
-- so it is deleted once no schedule and no unfinished job names it
-- (prune_definitions); a failed job lets go of its definition then. These
-- indexes serve that check and the foreign keys' checks on delete.
CREATE INDEX linch_worker_schedules_agent ON linch_worker_schedules (namespace, agent)
    WHERE agent IS NOT NULL;
CREATE INDEX linch_worker_jobs_agent ON linch_worker_jobs (namespace, agent)
    WHERE agent IS NOT NULL;

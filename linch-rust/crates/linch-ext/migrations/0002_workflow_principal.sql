-- The principal a workflow was started for, as JSON text; NULL for a
-- workflow whose calls act for nobody. A later claim must name the same
-- one, so a resume cannot run the journaled workflow under other rules.
ALTER TABLE linch_ext_workflows ADD COLUMN principal TEXT;

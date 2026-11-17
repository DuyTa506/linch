-- Who a run was started for (linch_core::Principal as JSON). A resume,
-- approve or answer that names no principal acts for this one, so a role
-- rule cannot be escaped by leaving the principal out of a continuation.
-- NULL: the run was started without one. Nullable with no default, so
-- adding it rewrites no rows.

ALTER TABLE linch_runs ADD COLUMN principal TEXT;

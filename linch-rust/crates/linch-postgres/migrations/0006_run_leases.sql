-- Run leases (PgRunStore). A claim records the store instance that holds
-- the run (`lease_holder`) and when its lease runs out (`lease_until`, on
-- the database clock); the holder's driver renews it on a timer. A claim
-- from another holder is refused while the run is `running` and
-- `lease_until` is in the future. NULL means no live lease: a run claimed
-- before this migration, or one whose holder released it. Both columns are
-- nullable with no default, so adding them rewrites no rows; they are not
-- indexed, so a renewal stays a heap-only update.

ALTER TABLE linch_runs
    ADD COLUMN lease_holder TEXT,
    ADD COLUMN lease_until  TIMESTAMPTZ;

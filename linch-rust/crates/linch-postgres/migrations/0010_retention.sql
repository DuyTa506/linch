-- Retention (linch_worker::prune_retained). Nothing here is read on the hot
-- path of a run: a run, a session append and a claim cost what they did.
-- Every statement is a catalog change or an index build over an empty set:
-- no existing row is rewritten, so the migration takes the same time on a
-- database of millions of runs as on an empty one (a backfilling UPDATE took
-- 15 s per 500k runs, and an agent's open gives a migration 30 s).
--
-- `ended_at` is when a run reached a terminal status, written by the commit
-- that moves it there (a terminal run is never written again). A run that
-- was terminal before this migration has none: the first retention pass
-- that has a window for runs stamps it then, so its window starts there.
--
-- `linch_runs_ended` serves the pruner's keyset scan over terminal runs,
-- oldest first. Only a terminal transition writes it (once per run); a
-- lease renewal, a claim and every other commit leave it alone.
-- `linch_runs_unstamped` holds only the rows that were terminal before
-- this migration, for the pass to stamp in batches; no new row enters it
-- (the terminal commit stamps the row it writes), so it empties as the
-- pass stamps them.
ALTER TABLE linch_runs ADD COLUMN ended_at TIMESTAMPTZ;

CREATE INDEX linch_runs_ended ON linch_runs (ended_at, namespace, run_id)
    WHERE ended_at IS NOT NULL;

CREATE INDEX linch_runs_unstamped ON linch_runs (namespace, run_id)
    WHERE ended_at IS NULL AND status IN ('completed', 'failed', 'aborted');

-- When a session last changed: its create and every append move it. It is
-- not indexed on purpose: an append already updates this row, and an index
-- on a column every append writes would end the heap-only updates. The
-- pruner scans sessions in key order instead. Existing sessions start
-- their clock at this migration (a column default is not a table rewrite).
ALTER TABLE linch_sessions ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- The runs a retention pass deleted, so a read of one says "pruned" and not
-- "never existed". Only the not-found path reads it. A row is kept for the
-- tombstone period of the retention config, then deleted.
CREATE TABLE linch_pruned_runs (
    namespace TEXT        NOT NULL,
    run_id    TEXT        NOT NULL,
    pruned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, run_id)
);

CREATE INDEX linch_pruned_runs_age ON linch_pruned_runs (pruned_at);

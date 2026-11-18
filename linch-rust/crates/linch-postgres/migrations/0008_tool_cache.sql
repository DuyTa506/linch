-- Tool results an agent reuses (linch_core::ToolCacheStore on PgToolCache).
-- An entry is keyed by the agent's namespace and the digest of the session,
-- tool name and canonical input; it is live until `expires_at`, the
-- database's clock, so servers with different clocks agree. `seq` orders
-- entries for eviction: the oldest go first when a namespace holds more than
-- its `max_entries` or a quota group more than its `max_bytes`.
-- `output` is TEXT, not JSONB, like every payload of these stores: a result
-- must come back byte for byte as it was stored. `bytes` is its length, and
-- `quota_group` (a tenant's name, set by the server's admission) the group
-- whose total the quota bounds.
-- There is no key to linch_sessions: the key digest covers the session when
-- the cache's scope says so.

CREATE TABLE linch_tool_cache (
    namespace   TEXT        NOT NULL,
    key         TEXT        NOT NULL,
    seq         BIGINT      GENERATED ALWAYS AS IDENTITY,
    output      TEXT        NOT NULL,
    bytes       BIGINT      NOT NULL,
    quota_group TEXT,
    expires_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE INDEX linch_tool_cache_order ON linch_tool_cache (namespace, seq);

-- A put sweeps a bounded batch of expired rows of any namespace.
CREATE INDEX linch_tool_cache_expiry ON linch_tool_cache (expires_at);

-- The quota trims a group newest first.
CREATE INDEX linch_tool_cache_quota ON linch_tool_cache (quota_group, seq)
    WHERE quota_group IS NOT NULL;

-- How often a namespace was cleared. A put is accepted only at the epoch its
-- lookup saw, so a read that began before a write cannot store its pre-write
-- answer after the write's clear. A row is never deleted: a cleared
-- namespace must not return to epoch 0.
CREATE TABLE linch_tool_cache_epoch (
    namespace TEXT   PRIMARY KEY,
    epoch     BIGINT NOT NULL
);

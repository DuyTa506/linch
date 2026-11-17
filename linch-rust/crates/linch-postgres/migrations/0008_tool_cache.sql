-- Tool results an agent reuses (linch_core::ToolCacheStore on PgToolCache).
-- An entry is keyed by the agent's namespace and the digest of the tool name
-- and canonical input; it is live until `expires_at`, the database's clock,
-- so servers with different clocks agree. `seq` orders entries for eviction:
-- the oldest go first when a namespace holds more than its `max_entries`.
-- `output` is TEXT, not JSONB, like every payload of these stores: a result
-- must come back byte for byte as it was stored.
-- There is no key to linch_sessions: the cache is shared by every session of
-- the namespace.

CREATE TABLE linch_tool_cache (
    namespace  TEXT        NOT NULL,
    key        TEXT        NOT NULL,
    seq        BIGINT      GENERATED ALWAYS AS IDENTITY,
    output     TEXT        NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE INDEX linch_tool_cache_order ON linch_tool_cache (namespace, seq);

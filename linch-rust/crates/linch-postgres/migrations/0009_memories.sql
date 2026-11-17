-- Long-term memories (linch_core::MemoryStore on PgMemoryStore). A row
-- belongs to one namespace and one scope: a NULL scope is the memory every
-- session of the agent shares, otherwise the scope names one principal.
-- `search` is a tsvector the writer builds from the words of the content
-- and tags (the 'simple' configuration: no stemming, no stop words, so any
-- language works); it is a plain column because building it from the tags
-- array is not an immutable expression. `call_key` is the saving tool
-- call's idempotency key, so a replayed call stores one row. No extension
-- is needed.

CREATE TABLE linch_memories (
    namespace  TEXT        NOT NULL,
    id         TEXT        NOT NULL,
    scope      TEXT,
    call_key   TEXT        NOT NULL,
    content    TEXT        NOT NULL,
    tags       TEXT[]      NOT NULL,
    search     TSVECTOR    NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, id),
    CHECK (scope IS NULL OR scope <> '')
);

CREATE UNIQUE INDEX linch_memories_call_key ON linch_memories (namespace, call_key);

CREATE INDEX linch_memories_search ON linch_memories USING GIN (search);

-- A tenant's cap counts the rows of its namespaces, the root first.
CREATE INDEX linch_memories_root ON linch_memories ((split_part(namespace, '/', 1)));

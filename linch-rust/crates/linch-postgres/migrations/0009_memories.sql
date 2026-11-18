-- Long-term memories (linch_core::MemoryStore on PgMemoryStore). A row
-- belongs to one namespace and one scope: a NULL scope is the memory every
-- session of the agent shares, otherwise the scope names one principal.
-- `terms` holds the words the writer extracts from the content and tags
-- (lower-cased, normal form C), each prefixed with a 16-hex-digit bucket
-- derived from the row's namespace and scope. The prefix is what keeps a
-- search inside its own namespace and scope *in the index*: a plain GIN
-- index over the words alone reads the posting list of a common word across
-- every tenant before any filter applies, while a query for `<bucket>word`
-- reads only that bucket's postings. It needs no extension (btree_gin would
-- do the same, but a migration that needs a contrib package breaks every
-- user of this crate who never uses memory). The WHERE clauses still name
-- the namespace and the scope exactly, so a bucket collision costs a wasted
-- posting and never a wrong row. `call_key` is the saving tool call's
-- idempotency key, so a replayed call stores one row.

CREATE TABLE linch_memories (
    namespace  TEXT        NOT NULL,
    id         TEXT        NOT NULL,
    scope      TEXT,
    call_key   TEXT        NOT NULL,
    content    TEXT        NOT NULL,
    tags       TEXT[]      NOT NULL,
    terms      TEXT[]      NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, id),
    CHECK (scope IS NULL OR scope <> '')
);

CREATE UNIQUE INDEX linch_memories_call_key ON linch_memories (namespace, call_key);

CREATE INDEX linch_memories_terms ON linch_memories USING GIN (terms);

-- A tenant's caps count the rows of its namespaces (the root first) and of
-- one scope within them.
CREATE INDEX linch_memories_root ON linch_memories ((split_part(namespace, '/', 1)), scope);

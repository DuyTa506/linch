-- Virtual files (linch_tools::vfs::FileBackend). Paths are COLLATE "C", so
-- the primary key orders them by bytes, the order every file backend lists
-- in, and serves prefix scans.
CREATE TABLE linch_files (
    namespace  TEXT        NOT NULL,
    path       TEXT        COLLATE "C" NOT NULL CHECK (path LIKE '/%'),
    content    TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, path)
);

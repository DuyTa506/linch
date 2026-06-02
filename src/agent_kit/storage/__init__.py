# Internal storage helpers — not part of the public API surface.
# SqliteExecutor is used by sessions/sqlite.py, memory/sqlite.py, and
# filesystem/sqlite.py to run blocking sqlite3 I/O off the event loop.

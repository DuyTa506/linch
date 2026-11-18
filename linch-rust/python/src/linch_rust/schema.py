"""`migrate` and `schema_status`: the database schema as a deploy step.

Both run the server binary once (`linch-server --migrate` or `--check`) and
return what the database holds. The DSN goes to it in an environment
variable, never on a command line.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from ._binary import find_server
from .errors import LinchError, OpenError, SchemaNewer

_DSN_VAR = "LINCH_SCHEMA_DSN"
_EXIT_NEWER = 3
_EXIT_BEHIND = 4
_TIMEOUT = 600.0


@dataclass(frozen=True)
class SchemaVersion:
    """The migrations of one table group, against what the server embeds.

    Attributes:
        applied: The newest applied migration; `None` while nothing is.
        latest: The newest migration the server binary embeds.
        current: Whether the applied migrations are exactly the embedded ones,
            as the server decided: a gap or a changed migration is not current
            though `applied == latest`.
    """

    applied: int | None
    latest: int
    current: bool

    @property
    def is_current(self) -> bool:
        """Whether every migration the server embeds is applied, and nothing else."""
        return self.current


@dataclass(frozen=True)
class SchemaStatus:
    """What a database has applied, for the stores and for the worker's queue.

    Attributes:
        store: Runs, sessions, files, mailbox, memories and the tool cache.
        worker: The worker's queue and schedule tables.
    """

    store: SchemaVersion
    worker: SchemaVersion

    @property
    def is_current(self) -> bool:
        """Whether the database is at the version the server binary embeds."""
        return self.store.is_current and self.worker.is_current


async def migrate(
    dsn: str,
    *,
    binary: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> SchemaStatus:
    """Apply every migration the server binary embeds, as a deploy step.

    Run it once per release, before starting servers on that release. It is
    safe to repeat and to run from several hosts at once. It needs a role
    with DDL rights; the servers then need none beyond reading and writing
    their tables.

    Args:
        dsn: The connection string the agents' `stores.dsn` uses, with its
            `search_path` schema when it has one.
        binary: The server binary; found by `find_server` when absent.
        env: Extra environment for the binary, such as `LINCH_LOG`; the DSN
            is added to it. Nothing else of this process's environment is
            passed on.

    Returns:
        The versions now applied; both are current.

    Raises:
        SchemaNewer: The database holds a migration this binary does not
            know; nothing was written. Upgrade the binary.
        OpenError: The database could not be reached or a migration failed.
        FileNotFoundError: No server binary was found.
    """
    return (await _run("--migrate", dsn, binary, env))[0]


async def schema_status(
    dsn: str,
    *,
    binary: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> SchemaStatus:
    """What the database has applied against the server binary, writing nothing.

    Args:
        dsn: As for `migrate`.
        binary: As for `migrate`.
        env: As for `migrate`.

    Returns:
        The versions; `is_current` is false for a database never migrated or
        behind the binary (run `migrate`).

    Raises:
        SchemaNewer: The database holds a migration this binary does not know.
        OpenError: The database could not be reached.
        FileNotFoundError: No server binary was found.
    """
    return (await _run("--check", dsn, binary, env))[0]


async def _run(
    flag: str,
    dsn: str,
    binary: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
) -> tuple[SchemaStatus, int]:
    path = binary if binary is not None else find_server()
    process = await asyncio.create_subprocess_exec(
        path,
        flag,
        "--dsn-env",
        _DSN_VAR,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**(env or {}), _DSN_VAR: dsn},
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), _TIMEOUT)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    code = process.returncode
    message = err.decode(errors="replace").strip().removeprefix("linch-server: ")
    if code == _EXIT_NEWER:
        raise SchemaNewer(-32016, message, _newer_data(out))
    if code not in (0, _EXIT_BEHIND):
        raise OpenError(-32014, message or f"linch-server exited with status {code}")
    try:
        report = json.loads(out)
    except ValueError as error:
        raise LinchError(f"linch-server printed no schema report: {out[:200]!r}") from error
    return _status(report), code


def _status(report: dict[str, Any]) -> SchemaStatus:
    def version(item: dict[str, Any]) -> SchemaVersion:
        applied = item["applied"]
        return SchemaVersion(
            None if applied is None else int(applied), int(item["latest"]), bool(item["current"])
        )

    return SchemaStatus(version(report["store"]), version(report["worker"]))


def _newer_data(out: bytes) -> dict[str, Any] | None:
    """The `{table, applied, latest}` of the server's `{"newer": ...}` line."""
    try:
        data: Any = json.loads(out)["newer"]
    except (ValueError, KeyError, TypeError):
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None

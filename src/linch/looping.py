from __future__ import annotations

import inspect
import json
import os
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypeAlias
from uuid import uuid4

from ._blocking import run_blocking
from .errors import ConfigError
from .events import ErrorEvent, Event, ResultEvent, SystemEvent
from .reports import RunReport, build_run_report
from .session import RunOptions

DonePredicate: TypeAlias = Callable[["LoopTickResult", "LoopArtifactStore"], bool | Awaitable[bool]]
VerifyPredicate: TypeAlias = Callable[["LoopTickResult"], bool | None | Awaitable[bool | None]]

# Refuse to follow a symlink at the lock path (Unix). 0 on platforms without it,
# which leaves behavior unchanged there.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(slots=True)
class LoopSpec:
    id: str
    charter: str
    prompt: str
    root: str | Path = "domains"
    run_options: RunOptions | None = None
    session_meta: dict[str, object] | None = None


@dataclass(slots=True)
class LoopTrigger:
    source: str = "manual"
    payload: str = ""
    id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class LoopTickResult:
    loop_id: str
    iteration: int
    run_id: str
    status: str
    done: bool
    final_text: str | None
    report: RunReport
    artifact_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LoopLease:
    loop_id: str
    owner: str
    token: str
    expires_at: float


class LoopArtifactStore(Protocol):
    async def ensure_layout(self, spec: LoopSpec) -> None: ...

    async def next_iteration(self, spec: LoopSpec) -> int: ...

    async def recent_log(self, spec: LoopSpec, *, max_chars: int = 4000) -> str: ...

    async def artifact_paths(self, spec: LoopSpec) -> list[str]: ...

    async def write_run_artifacts(
        self,
        spec: LoopSpec,
        *,
        run_id: str,
        report: RunReport,
    ) -> list[str]: ...

    async def append_tick(
        self,
        spec: LoopSpec,
        *,
        result: LoopTickResult,
        trigger: LoopTrigger,
        verification_error: str | None = None,
        run_error: BaseException | None = None,
    ) -> None: ...


class LoopLeaseStore(Protocol):
    async def try_acquire(
        self,
        loop_id: str,
        *,
        owner: str,
        ttl_s: float,
    ) -> LoopLease | None: ...

    async def refresh(self, lease: LoopLease, *, ttl_s: float) -> LoopLease: ...

    async def release(self, lease: LoopLease) -> None: ...


class FileLoopArtifactStore:
    """Filesystem-backed loop artifacts under ``<root>/<loop_id>/``.

    Each tick writes two files (``{run_id}.md`` / ``{run_id}.json``) into the
    loop's ``artifacts/runs/`` directory with no rotation or cap, so a
    long-lived loop grows disk/inode usage without bound. Retention is the
    operator's responsibility — prune the ``runs/`` directory (or point ``root``
    at managed storage) for loops expected to run for a long time.
    """

    def __init__(self, root: str | Path = "domains") -> None:
        self.root = Path(root)

    def _root_for(self, spec: LoopSpec) -> Path:
        root = Path(spec.root) if spec.root != "domains" else self.root
        return root / _safe_loop_id(spec.id)

    def _runs_dir(self, spec: LoopSpec) -> Path:
        return self._root_for(spec) / "artifacts" / "runs"

    async def ensure_layout(self, spec: LoopSpec) -> None:
        domain = self._root_for(spec)
        runs_dir = self._runs_dir(spec)

        def _ensure() -> None:
            runs_dir.mkdir(parents=True, exist_ok=True)
            readme = domain / "README.md"
            if not readme.exists():
                readme.write_text(_render_readme(spec), encoding="utf-8")
            log = domain / "LOG.md"
            if not log.exists():
                log.write_text(f"# Loop Log: {spec.id}\n\n", encoding="utf-8")

        await run_blocking(_ensure)

    async def next_iteration(self, spec: LoopSpec) -> int:
        runs_dir = self._runs_dir(spec)
        counter_path = self._root_for(spec) / "artifacts" / ".next_iteration"

        def _next() -> int:
            # Persist a monotonic counter so the per-tick cost stays O(1).
            # Globbing the runs dir is O(n) and n grows forever (one .json per
            # run lands in the same dir), so a long-lived loop slowed every tick.
            current: int | None = None
            if counter_path.exists():
                try:
                    current = int(counter_path.read_text(encoding="utf-8").strip())
                except (ValueError, OSError):
                    current = None
            if current is None:
                # Fresh domain, or migrating one created before the counter
                # existed: seed from the existing artifact count.
                current = len(list(runs_dir.glob("*.json"))) + 1 if runs_dir.exists() else 1
            counter_path.parent.mkdir(parents=True, exist_ok=True)
            counter_path.write_text(str(current + 1), encoding="utf-8")
            return current

        return await run_blocking(_next)

    async def recent_log(self, spec: LoopSpec, *, max_chars: int = 4000) -> str:
        log_path = self._root_for(spec) / "LOG.md"

        def _read() -> str:
            if not log_path.exists():
                return ""
            text = log_path.read_text(encoding="utf-8")
            return text[-max_chars:]

        return await run_blocking(_read)

    async def artifact_paths(self, spec: LoopSpec) -> list[str]:
        domain = self._root_for(spec)
        runs_dir = self._runs_dir(spec)

        def _paths() -> list[str]:
            paths = [
                domain / "README.md",
                domain / "LOG.md",
                runs_dir,
            ]
            if runs_dir.exists():
                paths.extend(sorted(runs_dir.glob("*"), key=lambda p: p.name)[-10:])
            return [str(path) for path in paths]

        return await run_blocking(_paths)

    async def write_run_artifacts(
        self,
        spec: LoopSpec,
        *,
        run_id: str,
        report: RunReport,
    ) -> list[str]:
        runs_dir = self._runs_dir(spec)
        md_path = runs_dir / f"{run_id}.md"
        json_path = runs_dir / f"{run_id}.json"

        def _write() -> list[str]:
            runs_dir.mkdir(parents=True, exist_ok=True)
            md_path.write_text(report.to_markdown() + "\n", encoding="utf-8")
            json_path.write_text(
                json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return [str(md_path), str(json_path)]

        return await run_blocking(_write)

    async def append_tick(
        self,
        spec: LoopSpec,
        *,
        result: LoopTickResult,
        trigger: LoopTrigger,
        verification_error: str | None = None,
        run_error: BaseException | None = None,
    ) -> None:
        log_path = self._root_for(spec) / "LOG.md"

        def _append() -> None:
            final = (result.final_text or "").strip().replace("\n", " ")
            if len(final) > 240:
                final = final[:237] + "..."
            lines = [
                f"## Tick {result.iteration}: {result.run_id}",
                "",
                f"- time: {_utc_now()}",
                f"- trigger: {trigger.source}",
                f"- status: {result.status}",
                f"- done: {result.done}",
                f"- events: {result.report.event_count}",
                f"- artifacts: {', '.join(result.artifact_paths)}",
            ]
            if trigger.id:
                lines.append(f"- trigger_id: {trigger.id}")
            if verification_error:
                lines.append(f"- verification_error: {verification_error}")
            if run_error is not None:
                lines.append(f"- run_error: {type(run_error).__name__}: {run_error}")
            if final:
                lines.extend(["", final])
            lines.append("")
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

        await run_blocking(_append)


class InMemoryLoopLeaseStore:
    def __init__(self) -> None:
        self._leases: dict[str, LoopLease] = {}
        self._guard = threading.Lock()

    async def try_acquire(
        self,
        loop_id: str,
        *,
        owner: str,
        ttl_s: float,
    ) -> LoopLease | None:
        _safe_loop_id(loop_id)
        now = time.time()
        with self._guard:
            current = self._leases.get(loop_id)
            if current is not None and current.expires_at > now:
                return None
            lease = LoopLease(
                loop_id=loop_id,
                owner=owner,
                token=str(uuid4()),
                expires_at=now + _validate_ttl(ttl_s),
            )
            self._leases[loop_id] = lease
            return lease

    async def refresh(self, lease: LoopLease, *, ttl_s: float) -> LoopLease:
        _safe_loop_id(lease.loop_id)
        with self._guard:
            current = self._leases.get(lease.loop_id)
            if current is None or current.token != lease.token:
                raise ConfigError(f"Loop lease for {lease.loop_id!r} is not held by this owner")
            refreshed = LoopLease(
                loop_id=lease.loop_id,
                owner=lease.owner,
                token=lease.token,
                expires_at=time.time() + _validate_ttl(ttl_s),
            )
            self._leases[lease.loop_id] = refreshed
            return refreshed

    async def release(self, lease: LoopLease) -> None:
        _safe_loop_id(lease.loop_id)
        with self._guard:
            current = self._leases.get(lease.loop_id)
            if current is not None and current.token == lease.token:
                self._leases.pop(lease.loop_id, None)


class FileLoopLeaseStore:
    def __init__(self, root: str | Path = "domains") -> None:
        self.root = Path(root)

    def _lock_path(self, loop_id: str) -> Path:
        return self.root / _safe_loop_id(loop_id) / ".lock.json"

    async def try_acquire(
        self,
        loop_id: str,
        *,
        owner: str,
        ttl_s: float,
    ) -> LoopLease | None:
        ttl = _validate_ttl(ttl_s)
        path = self._lock_path(loop_id)

        def _acquire() -> LoopLease | None:
            path.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            existing = _read_lease_file(path)
            if existing is not None and existing.expires_at > now:
                return None
            if existing is not None or path.exists():
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            lease = LoopLease(
                loop_id=loop_id,
                owner=owner,
                token=str(uuid4()),
                expires_at=now + ttl,
            )
            payload = _lease_to_json(lease)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
            except FileExistsError:
                return None
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            return lease

        return await run_blocking(_acquire)

    async def refresh(self, lease: LoopLease, *, ttl_s: float) -> LoopLease:
        ttl = _validate_ttl(ttl_s)
        path = self._lock_path(lease.loop_id)

        def _refresh() -> LoopLease:
            current = _read_lease_file(path)
            if current is None or current.token != lease.token:
                raise ConfigError(f"Loop lease for {lease.loop_id!r} is not held by this owner")
            refreshed = LoopLease(
                loop_id=lease.loop_id,
                owner=lease.owner,
                token=lease.token,
                expires_at=time.time() + ttl,
            )
            # O_NOFOLLOW: refuse to write the lease through a symlink swapped in
            # at the lock path after the read above (TOCTOU). O_EXCL only guards
            # the initial create in try_acquire, not this rewrite.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_lease_to_json(refreshed))
            return refreshed

        return await run_blocking(_refresh)

    async def release(self, lease: LoopLease) -> None:
        path = self._lock_path(lease.loop_id)

        def _release() -> None:
            current = _read_lease_file(path)
            if current is not None and current.token == lease.token:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

        await run_blocking(_release)


class LoopRunner:
    def __init__(
        self,
        agent: Any,
        *,
        artifacts: LoopArtifactStore | None = None,
        leases: LoopLeaseStore | None = None,
        lease_owner: str | None = None,
        lease_ttl_s: float = 300.0,
        done_predicate: DonePredicate | None = None,
        verify: VerifyPredicate | None = None,
    ) -> None:
        # Per-instance (not class-level) so two independent runners in one
        # process don't collide on the same spec.id — preserves the documented
        # "no process-global mutable state" multi-tenant guarantee.
        self._active_loop_ids: set[str] = set()
        self._active_guard = threading.Lock()
        self.agent = agent
        self.artifacts = artifacts or FileLoopArtifactStore()
        self.leases = leases
        self.lease_owner = lease_owner or _default_lease_owner()
        self.lease_ttl_s = _validate_ttl(lease_ttl_s)
        self.done_predicate = done_predicate
        self.verify = verify

    async def run_once(
        self,
        spec: LoopSpec,
        trigger: LoopTrigger | None = None,
    ) -> LoopTickResult:
        _validate_spec(spec)
        trigger = trigger or LoopTrigger()
        with self._active_guard:
            if spec.id in self._active_loop_ids:
                raise ConfigError(f"Loop {spec.id!r} already has an active run_once()")
            self._active_loop_ids.add(spec.id)
        lease: LoopLease | None = None
        try:
            if self.leases is not None:
                lease = await self.leases.try_acquire(
                    spec.id,
                    owner=self.lease_owner,
                    ttl_s=self.lease_ttl_s,
                )
                if lease is None:
                    raise ConfigError(f"Loop {spec.id!r} already has an active lease")
            return await self._run_once_locked(spec, trigger)
        finally:
            if lease is not None and self.leases is not None:
                await self.leases.release(lease)
            with self._active_guard:
                self._active_loop_ids.discard(spec.id)

    async def _run_once_locked(self, spec: LoopSpec, trigger: LoopTrigger) -> LoopTickResult:
        await self.artifacts.ensure_layout(spec)
        iteration = await self.artifacts.next_iteration(spec)
        artifact_paths = await self.artifacts.artifact_paths(spec)
        recent_log = await self.artifacts.recent_log(spec)
        prompt = _build_prompt(
            spec,
            trigger=trigger,
            artifact_paths=artifact_paths,
            recent_log=recent_log,
        )
        session_meta = _session_meta(spec, trigger=trigger, iteration=iteration)
        session = await self.agent.session(meta=session_meta)
        events: list[Event] = []
        run_error: Exception | None = None
        try:
            async for event in session.run(prompt, spec.run_options):
                events.append(event)
        except Exception as exc:
            run_error = exc
            events.append(
                ErrorEvent(
                    error={"name": type(exc).__name__, "message": str(exc), "retryable": False}
                )
            )
        finally:
            # Pop AND abort: popping alone leaves any background worker /
            # detached background-tool tasks spawned this tick running, which
            # could later write into a session the runner has discarded. abort()
            # cancels them (mirrors the evals harness teardown).
            sessions = getattr(self.agent, "_sessions", None)
            if isinstance(sessions, dict):
                sessions.pop(session.id, None)
            abort = getattr(session, "abort", None)
            if callable(abort):
                abort()

        report = build_run_report(events)
        if run_error is not None:
            report.status = "failed"
        run_id = report.run_id or _run_id_from_events(events)
        report.run_id = run_id
        report.session_id = report.session_id or session.id
        final_text = _final_text(events)
        status = report.status
        result = LoopTickResult(
            loop_id=spec.id,
            iteration=iteration,
            run_id=run_id,
            status=status,
            done=False,
            final_text=final_text,
            report=report,
            artifact_paths=[],
        )

        verification_error: str | None = None
        try:
            if self.verify is not None:
                verified = await _maybe_await(self.verify(result))
                if verified is False:
                    verification_error = "verify returned False"
            if verification_error is None and self.done_predicate is not None:
                result.done = bool(await _maybe_await(self.done_predicate(result, self.artifacts)))
        except Exception as exc:
            verification_error = f"{type(exc).__name__}: {exc}"

        if verification_error is not None:
            result.done = False
            result.status = "verification_failed"

        result.artifact_paths = await self.artifacts.write_run_artifacts(
            spec,
            run_id=run_id,
            report=report,
        )
        await self.artifacts.append_tick(
            spec,
            result=result,
            trigger=trigger,
            verification_error=verification_error,
            run_error=run_error,
        )
        return result


def _safe_loop_id(loop_id: str) -> str:
    if not isinstance(loop_id, str) or not loop_id.strip():
        raise ConfigError("LoopSpec.id must be a non-empty string")
    path = Path(loop_id)
    if path.is_absolute() or len(path.parts) != 1 or loop_id in {".", ".."}:
        raise ConfigError("LoopSpec.id must be a single relative path segment")
    return loop_id


def _validate_ttl(ttl_s: float) -> float:
    try:
        ttl = float(ttl_s)
    except (TypeError, ValueError) as exc:
        raise ConfigError("lease_ttl_s must be a positive number") from exc
    if ttl <= 0:
        raise ConfigError("lease_ttl_s must be a positive number")
    return ttl


def _default_lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"


def _lease_to_json(lease: LoopLease) -> str:
    return (
        json.dumps(
            {
                "loop_id": lease.loop_id,
                "owner": lease.owner,
                "token": lease.token,
                "expires_at": lease.expires_at,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _read_lease_file(path: Path) -> LoopLease | None:
    try:
        # O_NOFOLLOW: never read the lock through a symlink a local attacker may
        # have planted at the lock path (defense-in-depth). A symlink there makes
        # os.open raise OSError (ELOOP), which we treat as "no valid lease".
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            raw = json.loads(fh.read())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    loop_id = raw.get("loop_id")
    owner = raw.get("owner")
    token = raw.get("token")
    expires_at = raw.get("expires_at")
    if not isinstance(loop_id, str) or not isinstance(owner, str) or not isinstance(token, str):
        return None
    if not isinstance(expires_at, (int, float, str)):
        return None
    try:
        expires = float(expires_at)
    except (TypeError, ValueError):
        return None
    return LoopLease(loop_id=loop_id, owner=owner, token=token, expires_at=expires)


def _validate_spec(spec: LoopSpec) -> None:
    _safe_loop_id(spec.id)
    if not isinstance(spec.charter, str) or not spec.charter.strip():
        raise ConfigError("LoopSpec.charter must be a non-empty string")
    if not isinstance(spec.prompt, str) or not spec.prompt.strip():
        raise ConfigError("LoopSpec.prompt must be a non-empty string")


def _render_readme(spec: LoopSpec) -> str:
    return "\n".join(
        [
            f"# Loop Domain: {spec.id}",
            "",
            "## Charter",
            "",
            spec.charter.strip(),
            "",
        ]
    )


def _build_prompt(
    spec: LoopSpec,
    *,
    trigger: LoopTrigger,
    artifact_paths: Sequence[str],
    recent_log: str,
) -> str:
    paths = "\n".join(f"- {path}" for path in artifact_paths) or "- <none>"
    metadata = json.dumps(trigger.metadata, sort_keys=True) if trigger.metadata else "{}"
    return "\n".join(
        [
            "<loop>",
            f"<id>{spec.id}</id>",
            "<charter>",
            spec.charter.strip(),
            "</charter>",
            "<trigger>",
            f"source: {trigger.source}",
            f"id: {trigger.id or ''}",
            f"metadata: {metadata}",
            trigger.payload,
            "</trigger>",
            "<artifacts>",
            paths,
            "</artifacts>",
            "<recent-log>",
            recent_log.strip(),
            "</recent-log>",
            "<task>",
            spec.prompt.strip(),
            "</task>",
            "</loop>",
        ]
    )


def _session_meta(spec: LoopSpec, *, trigger: LoopTrigger, iteration: int) -> dict[str, object]:
    meta = dict(spec.session_meta or {})
    meta.update(
        {
            "loop_id": spec.id,
            "loop_iteration": iteration,
            "loop_trigger_source": trigger.source,
        }
    )
    if trigger.id is not None:
        meta["loop_trigger_id"] = trigger.id
    return meta


def _run_id_from_events(events: Sequence[Event]) -> str:
    for event in events:
        if isinstance(event, SystemEvent):
            return event.run_id
    return str(uuid4())


def _final_text(events: Sequence[Event]) -> str | None:
    for event in reversed(events):
        if isinstance(event, ResultEvent):
            return event.final_text
    return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

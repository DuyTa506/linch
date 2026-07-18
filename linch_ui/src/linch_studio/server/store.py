"""Symlink-aware file-backed Studio project store."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import JsonValue

from linch_studio.spec import (
    MAX_INPUT_BYTES,
    Blueprint,
    Diagnostic,
    MigrationWarning,
    blueprint_digest,
    default_blueprint,
    dump_blueprint,
    has_errors,
    load_blueprint,
)

from .errors import (
    AuthoringFailed,
    CorruptProject,
    InvalidProjectId,
    ProjectAlreadyExists,
    ProjectNotFound,
    ProposalNotFound,
    StaleDigest,
    StructuralBlueprintRejected,
    UnsafeProjectPath,
)

PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PROPOSAL_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
BLUEPRINT_FILENAME = "linch-studio.yaml"
STATE_DIRECTORY = ".linch-studio"
LAYOUT_FILENAME = "layout.json"
MAX_LAYOUT_BYTES = 2_097_152
MAX_PROPOSAL_BYTES = 2_097_152

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    project_id: str
    yaml: str
    blueprint: Blueprint
    digest: str
    diagnostics: tuple[Diagnostic, ...]
    updated_at: str
    migrated_from: str | None = None
    migration_warnings: tuple[MigrationWarning, ...] = ()

    @property
    def export_ready(self) -> bool:
        return not has_errors(self.diagnostics)


@dataclass(frozen=True, slots=True)
class StoredProposal:
    id: str
    project_id: str
    base_digest: str
    candidate_yaml: str
    candidate: Blueprint
    candidate_digest: str
    semantic_diff: tuple[dict[str, JsonValue], ...]
    diagnostics: tuple[Diagnostic, ...]
    created_at: str
    summary: str | None = None

    @property
    def export_ready(self) -> bool:
        return not has_errors(self.diagnostics)


class FileProjectStore:
    """Local project state with canonical YAML, independent layout, and CAS writes."""

    def __init__(self, workspace: str | Path) -> None:
        requested = Path(workspace)
        if requested.is_symlink():
            raise UnsafeProjectPath
        requested.mkdir(parents=True, exist_ok=True)
        if requested.is_symlink() or not requested.is_dir():
            raise UnsafeProjectPath
        self.workspace = requested.resolve(strict=True)
        self._lock = threading.RLock()

    def list_projects(self) -> tuple[ProjectSnapshot, ...]:
        with self._lock:
            snapshots: list[ProjectSnapshot] = []
            for entry in sorted(self.workspace.iterdir(), key=lambda item: item.name):
                if (
                    entry.is_symlink()
                    or not entry.is_dir()
                    or PROJECT_ID_PATTERN.fullmatch(entry.name) is None
                ):
                    continue
                blueprint_path = entry / BLUEPRINT_FILENAME
                if not blueprint_path.is_file() or blueprint_path.is_symlink():
                    continue
                snapshots.append(self._open_locked(entry.name))
            return tuple(snapshots)

    def create_project(
        self,
        project_id: str,
        *,
        title: str | None = None,
        template: str = "agent",
    ) -> ProjectSnapshot:
        validate_project_id(project_id)
        with self._lock:
            root = self._candidate_root(project_id)
            try:
                root.mkdir(mode=0o755)
            except FileExistsError:
                if root.is_symlink():
                    raise UnsafeProjectPath from None
                raise ProjectAlreadyExists from None
            try:
                blueprint = default_blueprint(project_id, title=title, template=template)
                self._atomic_write(root / BLUEPRINT_FILENAME, dump_blueprint(blueprint).encode())
                state = self._state_directory(root, create=True)
                assert state is not None
                self._atomic_write(state / LAYOUT_FILENAME, _default_layout_bytes())
                return self._open_locked(project_id)
            except Exception:
                _remove_empty_project(root)
                raise

    def open_project(self, project_id: str) -> ProjectSnapshot:
        with self._lock:
            return self._open_locked(project_id)

    def save_blueprint(
        self,
        project_id: str,
        source: str,
        *,
        base_digest: str,
    ) -> ProjectSnapshot:
        with self._lock:
            root = self._project_root(project_id)
            return self._save_blueprint_locked(root, project_id, source, base_digest)

    def load_layout(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            root = self._project_root(project_id)
            state = self._state_directory(root, create=False)
            if state is None:
                return _default_layout()
            path = state / LAYOUT_FILENAME
            if not path.exists():
                return _default_layout()
            raw = self._read_file(path, limit=MAX_LAYOUT_BYTES)
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise CorruptProject from None
            if not isinstance(value, dict):
                raise CorruptProject
            return value

    def save_layout(self, project_id: str, layout: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            root = self._project_root(project_id)
            state = self._state_directory(root, create=True)
            assert state is not None
            try:
                data = (
                    json.dumps(
                        layout,
                        allow_nan=False,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ).encode("utf-8")
                    + b"\n"
                )
            except (TypeError, ValueError):
                raise CorruptProject from None
            if len(data) > MAX_LAYOUT_BYTES:
                raise CorruptProject
            self._atomic_write(state / LAYOUT_FILENAME, data)
            return json.loads(data)

    def save_proposal(
        self,
        project_id: str,
        *,
        base_digest: str,
        candidate: Blueprint,
        semantic_diff: tuple[dict[str, JsonValue], ...],
        summary: str | None = None,
    ) -> StoredProposal:
        with self._lock:
            root = self._project_root(project_id)
            proposal_id = uuid4().hex
            candidate_yaml = dump_blueprint(candidate)
            created_at = datetime.now(timezone.utc).isoformat()
            payload: dict[str, Any] = {
                "schemaVersion": 1,
                "id": proposal_id,
                "baseDigest": base_digest,
                "candidateYaml": candidate_yaml,
                "semanticDiff": list(semantic_diff),
                "createdAt": created_at,
            }
            if summary is not None:
                payload["summary"] = summary
            try:
                encoded = json.dumps(
                    payload,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            except (TypeError, ValueError):
                raise AuthoringFailed from None
            if len(encoded) > MAX_PROPOSAL_BYTES:
                raise AuthoringFailed
            directory = self._proposal_directory(root, create=True)
            assert directory is not None
            self._atomic_write(directory / f"{proposal_id}.json", encoded)
            return self._get_proposal_locked(root, project_id, proposal_id)

    def list_proposals(self, project_id: str) -> tuple[StoredProposal, ...]:
        with self._lock:
            root = self._project_root(project_id)
            directory = self._proposal_directory(root, create=False)
            if directory is None:
                return ()
            proposals: list[StoredProposal] = []
            for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
                if path.is_symlink() or PROPOSAL_ID_PATTERN.fullmatch(path.stem) is None:
                    continue
                proposals.append(self._get_proposal_locked(root, project_id, path.stem))
            return tuple(proposals)

    def get_proposal(self, project_id: str, proposal_id: str) -> StoredProposal:
        with self._lock:
            root = self._project_root(project_id)
            return self._get_proposal_locked(root, project_id, proposal_id)

    def delete_proposal(self, project_id: str, proposal_id: str) -> None:
        validate_proposal_id(proposal_id)
        with self._lock:
            root = self._project_root(project_id)
            directory = self._proposal_directory(root, create=False)
            if directory is None:
                raise ProposalNotFound
            path = directory / f"{proposal_id}.json"
            if path.is_symlink():
                raise UnsafeProjectPath
            try:
                path.unlink()
            except FileNotFoundError:
                raise ProposalNotFound from None

    def accept_proposal(self, project_id: str, proposal_id: str) -> ProjectSnapshot:
        """Atomically compare the proposal base, save the whole candidate, and delete it."""

        with self._lock:
            root = self._project_root(project_id)
            proposal = self._get_proposal_locked(root, project_id, proposal_id)
            current = self._open_locked(project_id)
            if current.digest != proposal.base_digest:
                raise StaleDigest(current_digest=current.digest)
            saved = self._save_blueprint_locked(
                root,
                project_id,
                proposal.candidate_yaml,
                proposal.base_digest,
            )
            directory = self._proposal_directory(root, create=False)
            assert directory is not None
            (directory / f"{proposal_id}.json").unlink()
            return saved

    def _open_locked(self, project_id: str) -> ProjectSnapshot:
        root = self._project_root(project_id)
        path = root / BLUEPRINT_FILENAME
        raw = self._read_file(path, limit=MAX_INPUT_BYTES + 1)
        result = load_blueprint(raw)
        if result.blueprint is None:
            raise CorruptProject
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CorruptProject from None
        return ProjectSnapshot(
            project_id=project_id,
            yaml=source,
            blueprint=result.blueprint,
            digest=blueprint_digest(result.blueprint),
            diagnostics=result.diagnostics,
            updated_at=_mtime_iso(path),
            migrated_from=result.migrated_from,
            migration_warnings=result.migration_warnings,
        )

    def _save_blueprint_locked(
        self,
        root: Path,
        project_id: str,
        source: str,
        base_digest: str,
    ) -> ProjectSnapshot:
        current = self._open_locked(project_id)
        if current.digest != base_digest:
            raise StaleDigest(current_digest=current.digest)
        result = load_blueprint(source)
        if result.blueprint is None:
            raise StructuralBlueprintRejected(diagnostics=result.diagnostics)
        canonical_yaml = dump_blueprint(result.blueprint)
        blueprint_path = root / BLUEPRINT_FILENAME
        self._atomic_write(blueprint_path, canonical_yaml.encode("utf-8"))
        return ProjectSnapshot(
            project_id=project_id,
            yaml=canonical_yaml,
            blueprint=result.blueprint,
            digest=blueprint_digest(result.blueprint),
            diagnostics=result.diagnostics,
            updated_at=_mtime_iso(blueprint_path),
            migrated_from=result.migrated_from,
            migration_warnings=result.migration_warnings,
        )

    def _get_proposal_locked(
        self,
        root: Path,
        project_id: str,
        proposal_id: str,
    ) -> StoredProposal:
        validate_proposal_id(proposal_id)
        directory = self._proposal_directory(root, create=False)
        if directory is None:
            raise ProposalNotFound
        path = directory / f"{proposal_id}.json"
        if not path.exists():
            raise ProposalNotFound
        raw = self._read_file(path, limit=MAX_PROPOSAL_BYTES)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise TypeError
            if payload.get("id") != proposal_id or payload.get("schemaVersion") != 1:
                raise TypeError
            base_digest = payload["baseDigest"]
            candidate_yaml = payload["candidateYaml"]
            semantic_diff = payload["semanticDiff"]
            created_at = payload["createdAt"]
            summary = payload.get("summary")
            if (
                not isinstance(base_digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", base_digest) is None
                or not isinstance(candidate_yaml, str)
                or not isinstance(semantic_diff, list)
                or not all(isinstance(item, dict) for item in semantic_diff)
                or not isinstance(created_at, str)
                or not (summary is None or isinstance(summary, str))
            ):
                raise TypeError
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            raise CorruptProject from None
        result = load_blueprint(candidate_yaml)
        if result.blueprint is None:
            raise CorruptProject
        return StoredProposal(
            id=proposal_id,
            project_id=project_id,
            base_digest=base_digest,
            candidate_yaml=dump_blueprint(result.blueprint),
            candidate=result.blueprint,
            candidate_digest=blueprint_digest(result.blueprint),
            semantic_diff=tuple(semantic_diff),
            diagnostics=result.diagnostics,
            created_at=created_at,
            summary=summary,
        )

    def _candidate_root(self, project_id: str) -> Path:
        validate_project_id(project_id)
        candidate = self.workspace / project_id
        resolved = candidate.resolve(strict=False)
        if resolved.parent != self.workspace:
            raise UnsafeProjectPath
        return candidate

    def _project_root(self, project_id: str) -> Path:
        candidate = self._candidate_root(project_id)
        if candidate.is_symlink():
            raise UnsafeProjectPath
        if not candidate.exists():
            raise ProjectNotFound
        if not candidate.is_dir() or candidate.resolve(strict=True).parent != self.workspace:
            raise UnsafeProjectPath
        return candidate

    def _state_directory(self, root: Path, *, create: bool) -> Path | None:
        state = root / STATE_DIRECTORY
        if state.is_symlink():
            raise UnsafeProjectPath
        if not state.exists():
            if not create:
                return None
            try:
                state.mkdir(mode=0o700)
            except FileExistsError:
                if state.is_symlink() or not state.is_dir():
                    raise UnsafeProjectPath from None
        if not state.is_dir() or state.resolve(strict=True).parent != root.resolve(strict=True):
            raise UnsafeProjectPath
        return state

    def _proposal_directory(self, root: Path, *, create: bool) -> Path | None:
        state = self._state_directory(root, create=create)
        if state is None:
            return None
        directory = state / "proposals"
        if directory.is_symlink():
            raise UnsafeProjectPath
        if not directory.exists():
            if not create:
                return None
            directory.mkdir(mode=0o700)
        if not directory.is_dir() or directory.resolve(strict=True).parent != state.resolve(
            strict=True
        ):
            raise UnsafeProjectPath
        return directory

    def _read_file(self, path: Path, *, limit: int) -> bytes:
        if path.is_symlink():
            raise UnsafeProjectPath
        try:
            fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        except FileNotFoundError:
            raise CorruptProject from None
        except OSError:
            raise UnsafeProjectPath from None
        try:
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(limit + 1)
        except OSError:
            raise CorruptProject from None
        if len(data) > limit:
            raise CorruptProject
        return data

    def _atomic_write(self, path: Path, data: bytes) -> None:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise UnsafeProjectPath
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if path.is_symlink():
                raise UnsafeProjectPath
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def validate_project_id(project_id: str) -> None:
    if not isinstance(project_id, str) or PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise InvalidProjectId


def validate_proposal_id(proposal_id: str) -> None:
    if not isinstance(proposal_id, str) or PROPOSAL_ID_PATTERN.fullmatch(proposal_id) is None:
        raise ProposalNotFound


def _mtime_iso(path: Path) -> str:
    """Blueprint mtime as ISO-8601 UTC; carries no filesystem path into responses."""

    try:
        stamp = path.stat().st_mtime
    except OSError:
        raise CorruptProject from None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


def _default_layout() -> dict[str, Any]:
    return {"nodes": [], "viewport": {"x": 0.0, "y": 0.0, "zoom": 1.0}}


def _default_layout_bytes() -> bytes:
    return (json.dumps(_default_layout(), indent=2, sort_keys=True) + "\n").encode("utf-8")


def _remove_empty_project(root: Path) -> None:
    """Best-effort cleanup only for a project this store just created."""

    try:
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file() and not path.is_symlink():
                path.unlink()
            elif path.is_dir() and not path.is_symlink():
                path.rmdir()
        root.rmdir()
    except OSError:
        pass


__all__ = [
    "BLUEPRINT_FILENAME",
    "LAYOUT_FILENAME",
    "PROJECT_ID_PATTERN",
    "STATE_DIRECTORY",
    "FileProjectStore",
    "ProjectSnapshot",
    "StoredProposal",
    "validate_project_id",
    "validate_proposal_id",
]

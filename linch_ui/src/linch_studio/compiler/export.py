"""Deterministic manifest construction and no-overwrite exporters."""

from __future__ import annotations

import ast
import hashlib
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from linch_studio._version import GENERATOR_VERSION

from .contributions import ContributionSet, FileContribution, validate_contribution_path
from .serializers import canonical_json

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class ExportError(RuntimeError):
    """A safe export could not be completed."""


@dataclass(frozen=True, slots=True)
class CompiledProject:
    files: tuple[FileContribution, ...]
    blueprint_digest: str
    selected_capabilities: tuple[str, ...]
    linch_constraint: str

    def file(self, path: str) -> FileContribution:
        safe = validate_contribution_path(path)
        for item in self.files:
            if item.path == safe:
                return item
        raise KeyError(path)

    def preview(self) -> list[dict[str, Any]]:
        return [
            {
                "path": item.path,
                "sha256": item.sha256,
                "size": len(item.content.encode("utf-8")),
                "capabilityId": item.capability_id,
                "content": item.content,
            }
            for item in self.files
        ]


def finalize_project(
    contributions: ContributionSet,
    *,
    blueprint_digest: str,
    selected_capabilities: tuple[str, ...],
    linch_constraint: str,
) -> CompiledProject:
    """Validate Python and append the non-recursive deterministic manifest."""

    files = contributions.sorted()
    for item in files:
        if item.path.endswith(".py"):
            try:
                ast.parse(item.content, filename=item.path)
            except SyntaxError as exc:
                raise ExportError(
                    f"generated Python failed to parse at {item.path}:{exc.lineno}: {exc.msg}"
                ) from exc
    manifest = {
        "schemaVersion": 1,
        "generator": GENERATOR_VERSION,
        "blueprintDigest": blueprint_digest,
        "targetLinch": linch_constraint,
        "selectedCapabilities": list(selected_capabilities),
        "files": [
            {
                "path": item.path,
                "sha256": item.sha256,
                "capabilityId": item.capability_id,
            }
            for item in files
        ],
    }
    contributions.add(
        FileContribution(
            ".linch-studio-manifest.json",
            canonical_json(manifest, indent=2),
            "studio.manifest",
        )
    )
    return CompiledProject(
        files=contributions.sorted(),
        blueprint_digest=blueprint_digest,
        selected_capabilities=selected_capabilities,
        linch_constraint=linch_constraint,
    )


def export_directory(project: CompiledProject, target: str | Path) -> Path:
    """Stage then atomically install a project at a nonexistent or empty directory."""

    destination = Path(target)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    _ensure_target_directory_is_available(destination)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.linch-studio-", dir=parent))
    try:
        for item in project.files:
            path = stage.joinpath(*item.path.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_new_file(path, item.content.encode("utf-8"))
        _install_staged_directory(stage, destination)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return destination


def export_zip(project: CompiledProject, target: str | Path) -> Path:
    """Write a byte-deterministic ZIP and refuse every existing destination."""

    destination = Path(target)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ExportError(f"refusing to overwrite existing ZIP {destination}")

    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{destination.name}.linch-studio-", suffix=".tmp", dir=parent
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w+b") as stream:
            with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_STORED) as archive:
                for item in project.files:
                    info = zipfile.ZipInfo(item.path, date_time=_ZIP_TIMESTAMP)
                    info.compress_type = zipfile.ZIP_STORED
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    info.flag_bits |= 0x800
                    archive.writestr(info, item.content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise ExportError(f"refusing to overwrite existing ZIP {destination}") from exc
        temp.unlink()
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        raise
    return destination


def deterministic_zip_bytes(project: CompiledProject) -> bytes:
    """Return ZIP bytes for API responses without touching the filesystem."""

    import io

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for item in project.files:
            info = zipfile.ZipInfo(item.path, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.flag_bits |= 0x800
            archive.writestr(info, item.content.encode("utf-8"))
    return stream.getvalue()


def project_fingerprint(project: CompiledProject) -> str:
    digest = hashlib.sha256()
    for item in project.files:
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _ensure_target_directory_is_available(destination: Path) -> None:
    if destination.is_symlink():
        raise ExportError(f"refusing symlink export target {destination}")
    if not destination.exists():
        return
    if not destination.is_dir():
        raise ExportError(f"export target exists and is not a directory: {destination}")
    if any(destination.iterdir()):
        raise ExportError(f"export target is not empty: {destination}")


def _install_staged_directory(stage: Path, destination: Path) -> None:
    if destination.is_symlink():
        raise ExportError(f"refusing symlink export target {destination}")
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ExportError(f"export target became unavailable: {destination}")
        destination.rmdir()
    try:
        os.rename(stage, destination)
    except FileExistsError as exc:
        raise ExportError(f"export target became unavailable: {destination}") from exc


def _write_new_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
    fd = os.open(path, flags, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()


__all__ = [
    "CompiledProject",
    "ExportError",
    "deterministic_zip_bytes",
    "export_directory",
    "export_zip",
    "finalize_project",
    "project_fingerprint",
]

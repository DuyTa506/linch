from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from linch_studio.compiler import (
    ContributionError,
    ExportError,
    FileContribution,
    compile_file,
    deterministic_zip_bytes,
    export_directory,
    export_zip,
)
from linch_studio.compiler.contributions import ContributionSet, validate_contribution_path

GOLDEN = Path(__file__).parents[1] / "golden" / "standard_agent.yaml"


@pytest.mark.parametrize(
    "unsafe",
    ["", "/absolute", "../escape", "a/../escape", "./relative", "a\\b", "C:drive", "nul\0x"],
)
def test_generated_paths_reject_unsafe_or_non_normalized_values(unsafe: str) -> None:
    with pytest.raises(ContributionError):
        validate_contribution_path(unsafe)


def test_generated_paths_reject_casefold_and_unicode_collisions() -> None:
    contributions = ContributionSet()
    contributions.add(
        FileContribution("src/Caf\N{LATIN SMALL LETTER E WITH ACUTE}.py", "x = 1\n", "a")
    )

    with pytest.raises(ContributionError, match="collision"):
        contributions.add(FileContribution("SRC/CAFE\N{COMBINING ACUTE ACCENT}.PY", "x = 2\n", "b"))


def test_directory_export_accepts_only_absent_or_empty_target(tmp_path: Path) -> None:
    project = compile_file(GOLDEN)
    absent = tmp_path / "absent"
    empty = tmp_path / "empty"
    empty.mkdir()

    export_directory(project, absent)
    export_directory(project, empty)
    assert (absent / ".linch-studio-manifest.json").is_file()
    assert (empty / ".linch-studio-manifest.json").is_file()
    with pytest.raises(ExportError, match="not empty"):
        export_directory(project, absent)


def test_directory_export_refuses_symlink_target(tmp_path: Path) -> None:
    project = compile_file(GOLDEN)
    actual = tmp_path / "actual"
    actual.mkdir()
    target = tmp_path / "linked"
    target.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ExportError, match="symlink"):
        export_directory(project, target)


def test_zip_export_is_deterministic_safe_and_never_overwrites(tmp_path: Path) -> None:
    project = compile_file(GOLDEN)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    export_zip(project, first)
    export_zip(project, second)
    assert first.read_bytes() == second.read_bytes() == deterministic_zip_bytes(project)
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert all(
            not name.startswith("/") and ".." not in Path(name).parts for name in archive.namelist()
        )

    with pytest.raises(ExportError, match="overwrite"):
        export_zip(project, first)


def test_zip_export_refuses_symlink_destination(tmp_path: Path) -> None:
    project = compile_file(GOLDEN)
    existing = tmp_path / "existing.zip"
    existing.write_bytes(b"not a zip")
    linked = tmp_path / "linked.zip"
    linked.symlink_to(existing)

    with pytest.raises(ExportError, match="overwrite"):
        export_zip(project, linked)

"""Inventory shared by fast checks of the documentation shipped in distributions."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHIPPED_PATHS = (
    "docs/usage",
    "docs/architecture",
    "docs/architecture.md",
    "docs/usage.md",
    "docs/migration-3.0.md",
    "docs/versioning.md",
    "examples",
)


def shipped_inventory(root: Path = ROOT) -> tuple[Path, ...]:
    """Return maintained files, excluding caches and private workspace state."""
    return tuple(
        sorted(
            path
            for name in SHIPPED_PATHS
            for path in ((root / name).rglob("*") if (root / name).is_dir() else [root / name])
            if path.is_file()
            and not any(
                (part.startswith(".") or part == "__pycache__")
                for part in path.relative_to(root).parts
            )
        )
    )


@pytest.fixture
def shipped_files() -> tuple[Path, ...]:
    return shipped_inventory()


@pytest.fixture
def shipped_markdown(shipped_files: tuple[Path, ...]) -> tuple[Path, ...]:
    return tuple(path for path in shipped_files if path.suffix == ".md")


def pytest_addoption(parser):
    parser.addoption(
        "--run-packaging", action="store_true", help="build and install docs artifacts"
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "packaging: opt-in distribution build/install checks")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-packaging"):
        for item in items:
            if "packaging" in item.keywords:
                item.add_marker(
                    pytest.mark.skip(reason="opt in with --run-packaging to build artifacts")
                )

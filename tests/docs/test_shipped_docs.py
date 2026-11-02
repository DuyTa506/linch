"""Fast shipped-docs checks; opt in to builds with:

pytest tests/docs/test_shipped_docs.py --run-packaging
"""

import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import venv
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAYOUT = (
    "docs/usage",
    "docs/architecture",
    "docs/architecture.md",
    "docs/usage.md",
    "docs/migration-3.0.md",
    "docs/versioning.md",
    "examples",
)


def test_wheel_shipped_layout() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    actual = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert not actual.get("force-include")
    assert set(actual["only-include"]) == {"src/linch", *LAYOUT}
    assert actual["sources"] == {
        "src": "",
        "docs": "linch/_docs/docs",
        "examples": "linch/_docs/examples",
    }


@pytest.mark.parametrize("packaged", [True, False])
def test_docs_path_prefers_packaged_then_checkout(tmp_path, monkeypatch, capsys, packaged) -> None:
    from linch.ext import scaffold

    source = tmp_path / "checkout/src/linch/ext/scaffold/__init__.py"
    source.parent.mkdir(parents=True)
    source.touch()
    checkout = tmp_path / "checkout"
    (checkout / "pyproject.toml").touch()
    (checkout / "docs/usage").mkdir(parents=True)
    (checkout / "docs/usage/quickstart.md").touch()
    (checkout / "examples").mkdir()
    expected = checkout
    if packaged:
        expected = tmp_path / "checkout/src/linch/_docs"
        (expected / "docs/usage").mkdir(parents=True)
        (expected / "docs/usage/quickstart.md").touch()
        (expected / "examples").mkdir()
    monkeypatch.setattr(scaffold, "__file__", str(source))
    monkeypatch.chdir(tmp_path)
    assert scaffold.main(["docs", "path"]) == 0
    assert capsys.readouterr().out.strip() == str(expected.resolve())


def test_docs_path_missing_is_actionable(tmp_path, monkeypatch, capsys) -> None:
    from linch.ext import scaffold

    monkeypatch.setattr(scaffold, "__file__", str(tmp_path / "linch/ext/scaffold/__init__.py"))
    assert scaffold.main(["docs", "path"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "reinstall" in captured.err.lower()
    assert "docs" in captured.err.lower()


def test_installed_package_rejects_unrelated_ancestor_docs(tmp_path, monkeypatch, capsys) -> None:
    from linch.ext import scaffold

    source = tmp_path / ".venv/lib/python3.11/site-packages/linch/ext/scaffold/__init__.py"
    source.parent.mkdir(parents=True)
    source.touch()
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "docs/usage").mkdir(parents=True)
    (tmp_path / "docs/usage/quickstart.md").touch()
    (tmp_path / "examples").mkdir()
    monkeypatch.setattr(scaffold, "__file__", str(source))
    assert scaffold.main(["docs", "path"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "reinstall" in captured.err.lower()


def relative_links(text: str) -> list[str]:
    """Read inline, reference-definition and HTML links, ignoring code fences."""
    text = re.sub(r"(?ms)^(`{3,}|~{3,}).*?^\1[^\n]*$", "", text)
    return [
        match.group(1) or match.group(2) or match.group(3)
        for match in re.finditer(
            r"\]\(<?([^\s)>]+)>?(?:\s+[^)]*)?\)|"
            r"(?m:^\s*\[[^\]]+\]:\s*<?([^\s>]+)>?)|"
            r"(?:href|src)=[\"']([^\"']+)[\"']",
            text,
        )
    ]


def test_link_parser_covers_reference_and_html_links() -> None:
    assert relative_links('[a](a.md)\n[b]: b.md\n<a href="c.md">c</a>') == ["a.md", "b.md", "c.md"]


def test_shipped_relative_links_resolve(shipped_files, shipped_markdown) -> None:
    files = set(shipped_files)
    directories = {
        parent for path in files for parent in path.parents if parent.is_relative_to(ROOT)
    }
    errors = []
    for page in shipped_markdown:
        for link in relative_links(page.read_text(encoding="utf-8")):
            url = urlsplit(link)
            if url.scheme or url.netloc or not url.path:
                continue
            target = (page.parent / unquote(url.path)).resolve()
            if target not in files | directories:
                errors.append(f"{page.relative_to(ROOT)}: {link}")
    assert not errors, "Links outside shipped documentation:\n" + "\n".join(errors)


@pytest.mark.packaging
def test_clean_wheel_install_outside_checkout(tmp_path, shipped_files) -> None:
    """Build only here; the other documentation tests need no build backend."""
    try:
        import hatchling.build  # noqa: F401
    except ImportError:
        pytest.skip("hatchling build backend is absent; wheel-install validation unavailable")
    # Copy tracked files only: never read local credentials or other private state.
    project = tmp_path / "project"
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    for name in filter(None, tracked):
        source = ROOT / name
        if source.is_file():
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    private = (
        "examples/.linch/state",
        "examples/.cache/trace",
        "examples/.token",
        "examples/.env",
        "docs/usage/.env.local",
        "examples/.claude/secret",
        "docs/architecture/.codex/state",
        "examples/.agents/secret",
        "examples/__pycache__/bad.pyc",
        "examples/cache.pyc",
        ".env",
        ".claude/private",
        ".codex/private",
        ".superpowers/private",
        "docs/superpowers/private.md",
        "docs/ROADMAP.md",
    )
    for name in private:
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic private fixture", encoding="utf-8")
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-t", "sdist", "-d", str(dist)],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        actual = {
            name.removeprefix("linch/_docs/")
            for name in archive.namelist()
            if name.startswith("linch/_docs/")
        }
    assert actual == {str(path.relative_to(ROOT)) for path in shipped_files}
    with tarfile.open(next(dist.glob("*.tar.gz"))) as archive:
        sdist_files = {str(Path(name).relative_to("linch-3.0.1")) for name in archive.getnames()}
    assert not set(private) & sdist_files
    assert {str(path.relative_to(ROOT)) for path in shipped_files} <= sdist_files
    clean = tmp_path / "clean"
    venv.EnvBuilder(with_pip=True).create(clean)
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    subprocess.run(
        [str(clean / "bin/python"), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [str(clean / "bin/linch"), "docs", "path"],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    docs = Path(result.stdout.strip())
    assert docs.is_absolute() and docs.is_relative_to(clean)
    assert (docs / "docs/usage/quickstart.md").is_file()
    assert (docs / "examples").is_dir()

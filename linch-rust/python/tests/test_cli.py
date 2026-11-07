"""The `linch-rust` console script: `new`, `add tool`, `docs path` and `skill install`."""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from linch_rust.cli import _scaffold, main

PYTHON_DIR = Path(__file__).resolve().parents[1]
SRC = PYTHON_DIR / "src"


def new_project(tmp_path: Path, name: str = "my-agent") -> Path:
    assert main(["new", name, "--dir", str(tmp_path)]) == 0
    return tmp_path / name


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bare_project(tmp_path: Path, name: str, packages: list[str], dependency: str) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    pyproject = f'[project]\nname = "{name}"\ndependencies = ["{dependency}"]\n'
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    for package in packages:
        (root / "src" / package).mkdir()
        (root / "src" / package / "__init__.py").write_text("", encoding="utf-8")
    return root


@pytest.mark.source
def test_the_console_script_ships_as_a_wheel_data_script() -> None:
    pyproject = tomllib.loads((PYTHON_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    script = PYTHON_DIR / pyproject["tool"]["maturin"]["data"] / "scripts" / "linch-rust"
    assert script.read_text(encoding="utf-8").startswith("#!python\n"), "installers rewrite it"
    proc = subprocess.run(
        [sys.executable, str(script), "docs", "path"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert (Path(proc.stdout.strip()) / "architecture" / "README.md").is_file()


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "new" in out and "add" in out and "docs" in out and "skill" in out


def test_the_module_runs_as_a_script() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "linch_rust.cli", "--help"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "new" in proc.stdout


def test_an_unknown_command_is_a_usage_error() -> None:
    assert main(["frobnicate"]) == 2


def test_new_creates_the_project_tree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    proj = new_project(tmp_path)
    expected = [
        "pyproject.toml",
        "README.md",
        ".gitignore",
        "src/my_agent/__init__.py",
        "src/my_agent/agent.py",
        "src/my_agent/tools/__init__.py",
        "src/my_agent/tools/greet.py",
        "tests/test_agent.py",
        "tests/test_greet.py",
        ".claude/skills/build-agent-with-linch-rust/SKILL.md",
        ".claude/skills/build-agent-with-linch-rust/references/design.md",
        ".agents/skills/build-agent-with-linch-rust/SKILL.md",
        ".agents/skills/build-agent-with-linch-rust/references/design.md",
    ]
    assert sorted(str(p.relative_to(proj)) for p in proj.rglob("*") if p.is_file()) == sorted(
        expected
    )
    out = capsys.readouterr().out
    assert f"created {proj / 'src/my_agent/agent.py'}" in out
    assert f"  cd {proj}\n" in out and "python -m my_agent.agent" in out
    pyproject = tomllib.loads((proj / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["name"] == "my-agent"
    assert pyproject["project"]["dependencies"] == ["linch-rust"]
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/my_agent"]


def test_the_generated_python_uses_only_the_client(tmp_path: Path) -> None:
    proj = new_project(tmp_path)
    imported: set[str] = set()
    for path in proj.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("linch"):
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names if a.name.startswith("linch"))
    assert imported == {"linch_rust", "linch_rust.scripted"}


def test_the_generated_tests_pass_offline_against_the_server(tmp_path: Path) -> None:
    proj = new_project(tmp_path, "offline-proof")
    assert main(["add", "tool", "web-search", "--dir", str(proj)]) == 0
    env = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=proj,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "7 passed" in proc.stdout, proc.stdout


@pytest.mark.parametrize(
    "name", ["1bad", "Class", "class", "linch-rust", "linch_rust", "has space", "-leading"]
)
def test_new_rejects_invalid_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    assert main(["new", name, "--dir", str(tmp_path)]) == 2
    assert "error" in capsys.readouterr().err
    assert not (tmp_path / name).exists()


def test_new_refuses_a_directory_that_is_not_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "demo"
    target.mkdir()
    (target / "x.txt").write_text("keep", encoding="utf-8")
    assert main(["new", "demo", "--dir", str(tmp_path)]) == 1
    assert "demo" in capsys.readouterr().err
    assert (target / "x.txt").read_text(encoding="utf-8") == "keep"
    assert not (target / "pyproject.toml").exists()


def test_new_in_the_working_directory_says_cd_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["new", "my-agent"]) == 0
    assert "  cd my-agent\n" in capsys.readouterr().out


def test_new_fills_an_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    assert main(["new", "demo", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / "demo" / "pyproject.toml").is_file()


async def test_add_tool_creates_a_stub_and_its_test(tmp_path: Path) -> None:
    proj = new_project(tmp_path)
    assert main(["add", "tool", "web-search", "--dir", str(proj / "src")]) == 0
    stub = proj / "src/my_agent/tools/web_search.py"
    assert (proj / "tests/test_web_search.py").is_file()

    declared = load(stub, "generated_web_search").web_search
    assert declared.spec()["name"] == "web_search"
    assert declared.spec()["scope"] == "read"
    assert await declared.fn(query="rust") == "web_search results for: rust"


def test_add_tool_refuses_to_overwrite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    proj = new_project(tmp_path)
    assert main(["add", "tool", "greet", "--dir", str(proj)]) == 1
    assert "refusing to overwrite" in capsys.readouterr().err


def test_add_tool_needs_a_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["add", "tool", "x", "--dir", str(tmp_path)]) == 1
    assert "no linch-rust project" in capsys.readouterr().err


def test_add_tool_skips_a_project_on_another_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = bare_project(tmp_path, "other", ["other"], dependency="linch>=3")
    assert main(["add", "tool", "x", "--dir", str(root)]) == 1
    assert "no linch-rust project" in capsys.readouterr().err
    assert not (root / "src/other/tools").exists()


def test_add_tool_picks_the_package_named_like_the_project(tmp_path: Path) -> None:
    root = bare_project(tmp_path, "beta-app", ["alpha", "beta_app"], dependency="linch_rust>=0.1")
    assert main(["add", "tool", "x", "--dir", str(root)]) == 0
    assert (root / "src/beta_app/tools/x.py").is_file()
    assert (root / "src/beta_app/tools/__init__.py").is_file()


def test_add_tool_refuses_an_ambiguous_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = bare_project(tmp_path, "gamma", ["alpha", "beta"], dependency="linch-rust")
    assert main(["add", "tool", "x", "--dir", str(root)]) == 1
    assert "ambiguous" in capsys.readouterr().err


def test_docs_path_prints_the_shipped_docs(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["docs", "path"]) == 0
    root = Path(capsys.readouterr().out.strip())
    assert (root / "architecture" / "README.md").is_file()


def test_docs_path_fails_without_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_scaffold, "docs_roots", lambda: [tmp_path])
    assert main(["docs", "path"]) == 1
    assert "docs not found" in capsys.readouterr().err

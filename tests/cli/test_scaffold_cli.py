"""Tests for the ``linch`` scaffolding CLI (``linch new`` / ``linch add tool``)."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from linch.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]


def _new_project(tmp_path: Path, name: str = "my-agent") -> Path:
    assert main(["new", name, "--dir", str(tmp_path)]) == 0
    return tmp_path / name


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _assert_only_top_level_linch_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("linch"):
            imported_modules.append(node.module)
        elif isinstance(node, ast.Import):
            imported_modules.extend(
                alias.name for alias in node.names if alias.name.startswith("linch")
            )
    assert imported_modules
    assert set(imported_modules) == {"linch"}


async def _final_text(agent) -> str:
    session = await agent.session()
    final = ""
    async for event in session.run("Say hello"):
        if event.type == "result":
            final = event.final_text
    await agent.close()
    return final


def _bare_project(tmp_path: Path, name: str, packages: list[str]) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    for pkg in packages:
        (root / "src" / pkg).mkdir()
        (root / "src" / pkg / "__init__.py").write_text("")
    return root


def test_console_script_declared():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'linch = "linch.cli:main"' in text


def test_help_returns_zero(capsys):
    assert main(["--help"]) == 0
    assert "new" in capsys.readouterr().out


def test_module_entrypoint():
    proc = subprocess.run(
        [sys.executable, "-m", "linch.cli", "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    assert proc.returncode == 0
    assert "new" in proc.stdout


def test_new_creates_project_tree(tmp_path):
    proj = _new_project(tmp_path)
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
    ]
    for rel in expected:
        assert (proj / rel).is_file(), rel


def test_new_prints_created_files(tmp_path, capsys):
    _new_project(tmp_path)
    out = capsys.readouterr().out
    for fragment in (
        "my-agent/pyproject.toml",
        "my-agent/README.md",
        "my-agent/src/my_agent/agent.py",
        "my-agent/src/my_agent/tools/greet.py",
        "my-agent/tests/test_agent.py",
        "my-agent/tests/test_greet.py",
    ):
        assert fragment in out
    assert "cd my-agent" in out


def test_new_generated_python_parses(tmp_path):
    proj = _new_project(tmp_path)
    py_files = sorted(proj.rglob("*.py"))
    assert len(py_files) == 6
    for path in py_files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_new_generated_python_uses_only_top_level_linch_imports(tmp_path):
    proj = _new_project(tmp_path)
    linch_consumers = [path for path in proj.rglob("*.py") if "from linch" in path.read_text()]
    assert linch_consumers
    for path in linch_consumers:
        _assert_only_top_level_linch_imports(path)


def test_new_kebab_maps_to_snake(tmp_path):
    proj = _new_project(tmp_path)
    text = (proj / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "my-agent"' in text
    assert 'packages = ["src/my_agent"]' in text
    assert (proj / "src" / "my_agent").is_dir()


@pytest.mark.parametrize("name", ["1bad", "Class", "class", "linch", "has space", "-leading"])
def test_new_rejects_invalid_names(tmp_path, capsys, name):
    assert main(["new", name, "--dir", str(tmp_path)]) == 2
    assert "error" in capsys.readouterr().err.lower()
    assert not (tmp_path / name).exists()


def test_new_refuses_existing_nonempty_dir(tmp_path, capsys):
    target = tmp_path / "demo"
    target.mkdir()
    (target / "x.txt").write_text("keep")
    assert main(["new", "demo", "--dir", str(tmp_path)]) == 1
    assert "demo" in capsys.readouterr().err
    assert (target / "x.txt").read_text() == "keep"
    assert not (target / "pyproject.toml").exists()


def test_new_allows_empty_existing_dir(tmp_path):
    (tmp_path / "demo").mkdir()
    assert main(["new", "demo", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / "demo" / "pyproject.toml").is_file()


async def test_generated_greet_tool_passes_contract(tmp_path):
    from linch import assert_tool_contract

    proj = _new_project(tmp_path)
    module = _load_module(proj / "src" / "my_agent" / "tools" / "greet.py", "generated_greet")
    result = await assert_tool_contract(module.greet, valid_input={"name": "Ada"}, invalid_input={})
    assert result.content == "Hello, Ada!"


async def test_generated_agent_runs_offline(tmp_path, monkeypatch):
    from linch import ScriptedProvider, TextTurn

    proj = _new_project(tmp_path, "offline-proof")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.syspath_prepend(str(proj / "src"))
    importlib.invalidate_caches()
    try:
        module = importlib.import_module("offline_proof.agent")
        agent = module.build_agent(provider=ScriptedProvider([TextTurn(text="scripted")]))
        assert await _final_text(agent) == "scripted"

        offline_agent = module.build_agent()
        assert await _final_text(offline_agent) == module.OFFLINE_REPLY
    finally:
        for key in [k for k in list(sys.modules) if k.split(".")[0] == "offline_proof"]:
            sys.modules.pop(key, None)


async def test_generated_agent_tool_wiring(tmp_path, monkeypatch):
    from linch import ScriptedProvider, TextTurn, ToolUseTurn

    proj = _new_project(tmp_path, "wiring-proof")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.syspath_prepend(str(proj / "src"))
    importlib.invalidate_caches()
    try:
        module = importlib.import_module("wiring_proof.agent")
        agent = module.build_agent(
            provider=ScriptedProvider(
                [
                    ToolUseTurn(tool_name="greet", tool_input={"name": "Linch"}),
                    TextTurn(text="done"),
                ]
            )
        )
        assert await _final_text(agent) == "done"
    finally:
        for key in [k for k in list(sys.modules) if k.split(".")[0] == "wiring_proof"]:
            sys.modules.pop(key, None)


async def test_add_tool_creates_stub_and_test(tmp_path):
    from linch import assert_tool_contract

    proj = _new_project(tmp_path)
    assert main(["add", "tool", "web-search", "--dir", str(proj)]) == 0
    tool_path = proj / "src" / "my_agent" / "tools" / "web_search.py"
    test_path = proj / "tests" / "test_web_search.py"
    assert tool_path.is_file()
    assert test_path.is_file()
    ast.parse(tool_path.read_text(encoding="utf-8"))
    ast.parse(test_path.read_text(encoding="utf-8"))
    _assert_only_top_level_linch_imports(tool_path)
    _assert_only_top_level_linch_imports(test_path)
    module = _load_module(tool_path, "generated_web_search")
    result = await assert_tool_contract(
        module.web_search, valid_input={"query": "example"}, invalid_input={}
    )
    assert result.is_error is False


def test_add_tool_refuses_existing(tmp_path, capsys):
    proj = _new_project(tmp_path)
    assert main(["add", "tool", "web-search", "--dir", str(proj)]) == 0
    tool_path = proj / "src" / "my_agent" / "tools" / "web_search.py"
    before = tool_path.read_text(encoding="utf-8")
    assert main(["add", "tool", "web-search", "--dir", str(proj)]) == 1
    assert "web_search.py" in capsys.readouterr().err
    assert tool_path.read_text(encoding="utf-8") == before


def test_add_tool_outside_project(tmp_path, capsys):
    assert main(["add", "tool", "lookup", "--dir", str(tmp_path)]) == 1
    assert "pyproject.toml" in capsys.readouterr().err


def test_add_tool_picks_package_matching_project_name(tmp_path):
    root = _bare_project(tmp_path, "a", ["a", "b"])
    assert main(["add", "tool", "lookup", "--dir", str(root)]) == 0
    assert (root / "src" / "a" / "tools" / "lookup.py").is_file()
    assert not (root / "src" / "b" / "tools").exists()


def test_add_tool_ambiguous_src_no_match(tmp_path, capsys):
    root = _bare_project(tmp_path, "zzz", ["a", "b"])
    assert main(["add", "tool", "lookup", "--dir", str(root)]) == 1
    err = capsys.readouterr().err
    assert "a" in err
    assert "b" in err

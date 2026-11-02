"""Guard generated imports, real signatures, and offline reference maintenance."""

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/generate_api_reference.py"
DOC = ROOT / "docs/usage/api-reference.md"


def load_generator():
    assert SCRIPT.is_file(), "static API reference generator is missing"
    spec = importlib.util.spec_from_file_location("api_reference_generator", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def literal_exports(path):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    return []


def entry(document, qualified):
    return document.split(f"### `{qualified}`\n", 1)[1].split("\n### ", 1)[0]


def test_reference_covers_every_declared_public_export_once():
    assert DOC.is_file(), "generated API reference is missing"
    expected = [f"linch.{name}" for name in literal_exports(ROOT / "src/linch/__init__.py")]
    for path in sorted((ROOT / "src/linch/ext").rglob("__init__.py")):
        module = ".".join(path.parent.relative_to(ROOT / "src").parts)
        expected.extend(f"{module}.{name}" for name in literal_exports(path))
    actual = re.findall(r"^### `([^`]+)`$", DOC.read_text(), re.M)
    assert len(actual) == len(set(actual))
    assert set(actual) == set(expected)
    assert len(actual) == 315


@pytest.mark.parametrize(
    ("name", "fragments"),
    [
        (
            "linch.connect_mcp_servers",
            [
                "from linch import connect_mcp_servers",
                "async def connect_mcp_servers(servers: dict[str, McpServerConfig]) "
                "-> McpConnection",
                "Connect to MCP servers and wrap their tools for a Linch registry.",
            ],
        ),
        (
            "linch.BeforeProviderCallContext",
            [
                "BeforeProviderCallContext(session: Any, run_id: str, turn_index: int | None, "
                "deps: Any = None, request: ProviderRequest | None = None, "
                "context_result: ContextBuildResult | None = None)",
                "Expose a provider request before dispatch.",
            ],
        ),
        (
            "linch.TextBlock",
            ["TextBlock(text: str, type: Literal['text'] = 'text')"],
        ),
        (
            "linch.RUN_SCHEMA_VERSION",
            ["from linch import RUN_SCHEMA_VERSION", "Constant", "RUN_SCHEMA_VERSION = 3"],
        ),
        (
            "linch.RunLeaseConflictError",
            ["Inherited initializer: RuntimeError.__init__(self, /, *args, **kwargs)"],
        ),
        (
            "linch.McpServerConfig",
            [
                "Kind: Type alias.",
                "McpServerConfig = McpStdioServerConfig | McpHttpServerConfig",
            ],
        ),
        (
            "linch.ModelId",
            ["Type alias", "ModelId: TypeAlias = str"],
        ),
        (
            "linch.ext.deep_agent.DEEP_AGENT_SYSTEM_PROMPT",
            ["from linch.ext.deep_agent import DEEP_AGENT_SYSTEM_PROMPT", "Constant"],
        ),
    ],
)
def test_reference_preserves_hand_checked_source_contracts(name, fragments):
    assert DOC.is_file(), "generated API reference is missing"
    block = entry(DOC.read_text(), name)
    for fragment in fragments:
        assert fragment in block


def test_type_alias_declarations_do_not_claim_runtime_aliases_are_uncallable():
    document = DOC.read_text()
    assert "ModelId: TypeAlias = str" in entry(document, "linch.ModelId")
    assert "not callable" not in entry(document, "linch.ModelId")
    assert "Constants and type aliases are not callable" not in document
    for block in document.split("\n### "):
        if "Kind: Type alias" in block:
            assert "not callable" not in block


def test_scaffold_main_links_to_cli_usage_instead_of_ordinary_main_prose():
    block = entry(DOC.read_text(), "linch.ext.scaffold.main")
    assert "Usage: [cli](cli.md)." in block
    assert "Usage: [agent](agent.md)." not in block


def test_check_is_offline_and_repeated_runs_are_identical(tmp_path, monkeypatch, capsys):
    generator = load_generator()
    target = tmp_path / "reference.md"
    monkeypatch.setattr(generator, "DOC", target)
    assert generator.main([]) == 0
    first = target.read_bytes()
    assert generator.main([]) == 0
    assert target.read_bytes() == first
    assert generator.main(["--check"]) == 0
    target.write_text("stale\n")
    assert generator.main(["--check"]) == 1
    assert "stale" in capsys.readouterr().err
    assert target.read_text() == "stale\n"
    checked = subprocess.run(
        [sys.executable, "-S", str(SCRIPT), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr


def test_static_resolution_reports_missing_docs_and_prefers_canonical_try_import(
    tmp_path, monkeypatch, capsys
):
    generator = load_generator()
    package = tmp_path / "linch"
    package.mkdir()
    (package / "__init__.py").write_text(
        "_EXPORTS = {'.bridge': ('public_alias',)}\n"
        "_ALIASES = {'public_alias': ('.bridge', 'original')}\n"
        "__all__ = ['public_alias']\n"
    )
    (package / "bridge.py").write_text(
        "try:\n from .impl import original\nexcept ImportError:\n"
        " def original(*args):\n  '''Fallback.'''\n"
    )
    (package / "impl.py").write_text("def original(value: int = 7) -> str:\n return str(value)\n")
    monkeypatch.setattr(generator, "SRC", tmp_path)
    monkeypatch.setattr(generator, "DOC", tmp_path / "reference.md")
    assert generator.main([]) == 1
    assert "public_alias" in capsys.readouterr().err
    (package / "impl.py").write_text(
        "def original(value: int = 7) -> str:\n '''Canonical function.'''\n return str(value)\n"
    )
    generator.module.cache_clear()
    assert generator.main([]) == 0
    result = generator.DOC.read_text()
    assert "def public_alias(value: int=7) -> str" in result
    assert "Canonical function." in result
    assert "Fallback." not in result

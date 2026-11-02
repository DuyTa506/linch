"""Compile shipped Python fences and enforce the documented import contract offline."""

import ast
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OFFLINE_MARKER = "<!-- linch:offline -->"

# Resolved imports must not keep exceptions that could hide future regressions.
unresolved: list[tuple[str, str, int, str]] = []


@dataclass(frozen=True, slots=True)
class Fence:
    path: Path
    line: int
    code: str
    offline: bool = False


def python_fences(path: Path) -> list[Fence]:
    """Read backtick/tilde fences, preserving source lines and explicit markers."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    result = []
    index = 0
    while index < len(lines):
        opening = re.fullmatch(r"( {0,3})(`{3,}|~{3,})([^\n]*)\n?", lines[index])
        if not opening:
            index += 1
            continue
        indent, delimiter, info = opening.groups()
        start = index + 1
        index = start
        while index < len(lines) and not re.fullmatch(
            rf" {{0,3}}{re.escape(delimiter[0])}{{{len(delimiter)},}}[ \t]*\n?",
            lines[index],
        ):
            index += 1
        assert index < len(lines), f"{path}:{start}: unclosed fence"
        if info.strip() == "python":
            previous = next(
                (line.strip() for line in reversed(lines[: start - 1]) if line.strip()), ""
            )
            result.append(
                Fence(
                    path,
                    start + 1,
                    "".join(line.removeprefix(indent) for line in lines[start:index]),
                    previous == OFFLINE_MARKER,
                )
            )
        index += 1
    return result


def inline_imports(path: Path) -> list[Fence]:
    """Check literal import recommendations in inline code as well as fences."""
    text = path.read_text(encoding="utf-8")
    return [
        Fence(path, text.count("\n", 0, match.start()) + 1, match.group(1))
        for match in re.finditer(r"`(from linch(?:\.[\w.]+)? import [^`\n]+)`", text)
    ]


def root_exports() -> set[str]:
    tree = ast.parse((ROOT / "src/linch/__init__.py").read_text(encoding="utf-8"))
    return next(
        set(ast.literal_eval(node.value))
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    )


def optional_modules() -> dict[str, str]:
    """Versioning's exact module table is the sole optional-path allowlist."""
    text = (ROOT / "docs/versioning.md").read_text(encoding="utf-8")
    return dict(re.findall(r"^\| `(linch\.[\w.]+)` \| `([\w-]+)` \|", text, re.M))


def import_issues(code: str, exports: set[str], optional: dict[str, str]) -> list[tuple[str, int]]:
    issues = []

    def allowed(module):
        return not any(part.startswith("_") for part in module.split(".")) and (
            module in optional or module.startswith("linch.ext.")
        )

    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level and not module:
                issues.extend(
                    ("." * node.level + alias.name, alias.lineno)
                    for alias in node.names
                    if alias.name == "linch"
                )
            if module != "linch" and not module.startswith("linch."):
                continue
            for alias in node.names:
                if (
                    node.level
                    or alias.name == "*"
                    or not (
                        alias.name in exports
                        if module == "linch"
                        else (
                            allowed(module)
                            and not alias.name.startswith("_")
                            and (module.startswith("linch.ext.") or alias.name not in exports)
                        )
                    )
                ):
                    issues.append((f"{'.' * node.level}{module}.{alias.name}", alias.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("linch.") and not allowed(alias.name):
                    issues.append((alias.name, alias.lineno))
    return issues


def unexpected_imports(actual, exceptions):
    actual, exceptions = Counter(actual), Counter(exceptions)
    return actual - exceptions, exceptions - actual


def import_occurrences(fence, exports, optional):
    nodes = [
        node
        for node in ast.walk(ast.parse(fence.code))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    return [
        (
            name,
            str(fence.path.relative_to(ROOT)),
            fence.line + line - 1,
            ast.get_source_segment(
                fence.code, next(node for node in nodes if node.lineno <= line <= node.end_lineno)
            ),
        )
        for name, line in import_issues(fence.code, exports, optional)
    ]


def test_fence_parser_covers_whole_pages_and_preserves_lines(tmp_path):
    page = tmp_path / "example.md"
    page.write_text(
        "# Page\n\n```text\n```python\nhidden\n```\n\n"
        "<!-- linch:offline -->\n\n~~~~python\nfrom linch import Agent\n~~~~\n"
        "\n   ```python\n   await work()\n   ```\n"
    )
    fences = python_fences(page)
    assert [(f.line, f.offline, f.code) for f in fences] == [
        (11, True, "from linch import Agent\n"),
        (15, False, "await work()\n"),
    ]


@pytest.mark.parametrize(
    ("code", "invalid"),
    [
        ("from linch import Agent as App", []),
        ("import linch", []),
        ("from linch import invented as Agent", ["linch.invented"]),
        ("from linch import *", ["linch.*"]),
        ("from linch.tools import Agent", ["linch.tools.Agent"]),
        ("import linch.tools as tools", ["linch.tools"]),
        ("import linch.worker.internal", ["linch.worker.internal"]),
        ("from linch.worker.internal import Worker", ["linch.worker.internal.Worker"]),
        ("from linch.worker import Worker as Runtime", []),
        ("from linch.worker import Agent", ["linch.worker.Agent"]),
        ("from linch.ext.evals import ScriptedProvider", []),
        ("from linch.ext.evals import *", ["linch.ext.evals.*"]),
        ("from linch.extension import Agent", ["linch.extension.Agent"]),
        ("from .linch import Agent", [".linch.Agent"]),
        ("from . import linch as sdk", [".linch"]),
        ("from ..linch.worker import Worker", ["..linch.worker.Worker"]),
        ("from linch.worker import _find_spec as discover", ["linch.worker._find_spec"]),
        ("from linch.sessions.postgres import _record", ["linch.sessions.postgres._record"]),
        ("from linch.ext._private import Adapter", ["linch.ext._private.Adapter"]),
        ("import linch.ext._private as adapter", ["linch.ext._private"]),
        ("from linch.ext.evals import _private", ["linch.ext.evals._private"]),
        ("from linch import __version__ as version", []),
        ("from linch import Agent as App, __version__ as version", []),
        (
            "from linch.worker import Worker as Runtime, _find_spec as discover",
            ["linch.worker._find_spec"],
        ),
    ],
)
def test_import_checker_rejects_private_paths_and_undeclared_root_names(code, invalid):
    assert [
        name
        for name, _ in import_issues(
            code,
            {"Agent", "__version__"},
            {"linch.worker": "worker", "linch.sessions.postgres": "postgres"},
        )
    ] == invalid


def test_unresolved_work_list_rejects_both_new_and_stale_exceptions():
    assert unexpected_imports([("linch.private.New", "a.md")], []) == (
        Counter({("linch.private.New", "a.md"): 1}),
        Counter(),
    )
    assert unexpected_imports([], unresolved)[1] == Counter(unresolved)


@pytest.mark.parametrize(
    ("line", "source"),
    [
        (45, "from linch.tools.execution import DockerBackend"),
        (40, "from linch.tools.execution import DockerBackend as Replacement"),
    ],
)
def test_unresolved_identity_rejects_same_page_relocation_and_replacement(line, source):
    original = Fence(
        ROOT / "docs/usage/tools.md", 40, "from linch.tools.execution import DockerBackend"
    )
    expected = [("linch.tools.execution.DockerBackend", "docs/usage/tools.md", 40, original.code)]
    assert import_occurrences(original, set(), {}) == expected
    replacement = Fence(original.path, line, source)
    new, stale = unexpected_imports(import_occurrences(replacement, set(), {}), expected)
    assert new and stale


def test_provider_contract_guide_recommends_supported_import_path():
    guide = (ROOT / "docs/architecture/provider-contract.md").read_text()
    assert "`linch.testing`" not in guide
    assert "`linch`:" in guide


def test_inline_import_parser_keeps_the_page_location(tmp_path):
    page = tmp_path / "inline.md"
    page.write_text("# Migration\n\nUse `from linch.private import Hidden`.\n")
    assert inline_imports(page) == [Fence(page, 3, "from linch.private import Hidden")]


def test_supported_optional_modules_have_real_extra_and_source():
    modules = optional_modules()
    assert modules, "document supported optional-extra import modules in versioning"
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]
    for module, extra in modules.items():
        assert extra in extras
        path = ROOT / "src" / module.replace(".", "/")
        source = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
        assert source.is_file()
        assert f"linch[{extra}]" in source.read_text()


def test_compilation_allows_top_level_await_but_rejects_duplicate_keywords():
    compile("await run()", "example.md", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    with pytest.raises(SyntaxError, match="keyword argument repeated"):
        compile(
            "Agent(result_offload=1, result_offload=2)",
            "example.md",
            "exec",
            flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )


def test_every_shipped_python_fence_compiles(shipped_markdown):
    errors = []
    for page in shipped_markdown:
        for fence in python_fences(page):
            try:
                compile(fence.code, str(page), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            except SyntaxError as error:
                errors.append(
                    f"{page.relative_to(ROOT)}:{fence.line + error.lineno - 1}: {error.msg}"
                )
    assert not errors, "Invalid Python fences:\n" + "\n".join(errors)


def test_every_shipped_python_import_matches_contract(shipped_markdown):
    exports, optional = root_exports(), optional_modules()
    found, locations = [], []
    for page in shipped_markdown:
        for fence in python_fences(page) + inline_imports(page):
            for occurrence in import_occurrences(fence, exports, optional):
                name, path, line, _ = occurrence
                found.append(occurrence)
                locations.append(f"{path}:{line}: {name}")
    new, stale = unexpected_imports(found, unresolved)
    assert not new and not stale, (
        f"Unsupported imports: {new}\nStale unresolved entries: {stale}\n" + "\n".join(locations)
    )


def test_every_shipped_example_import_matches_contract():
    exports, optional = root_exports(), optional_modules()
    errors = []
    for path in sorted((ROOT / "examples").rglob("*.py")):
        code = path.read_text(encoding="utf-8")
        compile(code, str(path), "exec")
        errors.extend(
            f"{path.relative_to(ROOT)}:{line}: {name}"
            for name, line in import_issues(code, exports, optional)
        )
    assert not errors, "Unsupported example imports:\n" + "\n".join(errors)


def test_example_module_run_commands_name_the_real_file():
    errors = []
    for path in sorted((ROOT / "examples").rglob("*.py")):
        docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        for command_path in re.findall(r"\bpython(?:3)?\s+(examples/[\w./-]+\.py)", docstring):
            if ROOT / command_path != path:
                errors.append(f"{path.relative_to(ROOT)}: {command_path}")
    assert not errors, "Stale example run commands:\n" + "\n".join(errors)


def test_marked_quickstart_runs_without_network_credentials_or_optional_extras(tmp_path):
    page = ROOT / "docs/usage/quickstart.md"
    fences = [fence for fence in python_fences(page) if fence.offline]
    assert fences, "mark the existing offline quickstart with <!-- linch:offline -->"
    # Simulate a core-only install even when the developer environment has extras.
    extras = {"anthropic", "asyncpg", "google", "mcp", "pgqueuer", "psycopg", "psycopg_pool"}
    guard = (
        "import sys, socket\n"
        "class NoExtras:\n"
        " def find_spec(self, fullname, path=None, target=None):\n"
        f"  if fullname.split('.')[0] in {extras!r}:\n"
        "   raise ModuleNotFoundError('offline quickstart loaded optional extra: ' + fullname)\n"
        "sys.meta_path.insert(0, NoExtras())\n"
        "def no_network(*args, **kwargs):\n"
        " raise AssertionError('offline quickstart attempted a network connection')\n"
        "socket.socket.connect = no_network\n"
        "socket.socket.connect_ex = no_network\n"
        "socket.create_connection = no_network\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(secret in key for secret in ("API_KEY", "TOKEN", "CREDENTIAL", "DSN"))
    }
    env["PYTHONPATH"] = str(ROOT / "src")
    for fence in fences:
        result = subprocess.run(
            [sys.executable, "-c", guard + fence.code],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, f"{page}:{fence.line}: {result.stderr}"
        assert result.stdout.strip() == "Hello from Linch."

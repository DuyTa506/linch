"""Generate the checked-in typed lifecycle catalog in the architecture guide."""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from linch.lifecycle import LifecyclePoint

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DOC = ROOT / "docs/architecture/events.md"
BEGIN = "<!-- BEGIN GENERATED LIFECYCLE CATALOG -->"
END = "<!-- END GENERATED LIFECYCLE CATALOG -->"


def _load_points() -> tuple[dict[int, str], tuple[LifecyclePoint, ...]]:
    sys.path.insert(0, str(SRC))
    from linch import lifecycle

    names = {
        id(point): name
        for name, point in vars(lifecycle).items()
        if lifecycle.LifecyclePoint is type(point)
    }
    return names, lifecycle.LIFECYCLE_POINTS


def _symbol(path: Path, stack: list[str]) -> str:
    module = ".".join(path.relative_to(SRC).with_suffix("").parts)
    if not stack:
        return module
    if stack[0].lstrip("_")[:1].isupper() and len(stack) > 1:
        return f"{module}:{stack[0]}.{stack[1]}"
    return f"{module}:{stack[0]}"


class _SourceVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, points: set[str]) -> None:
        self.path = path
        self.points = points
        self.aliases: dict[str, str] = {}
        self.stack: list[str] = []
        self.producers: dict[str, set[str]] = defaultdict(set)
        self.consumers: dict[str, set[str]] = defaultdict(set)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.endswith(".lifecycle"):
                self.aliases[alias.asname or alias.name.rsplit(".", 1)[-1]] = "*"

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        is_lifecycle = bool(node.module and node.module.endswith("lifecycle"))
        is_root_lifecycle = node.module in {None, "linch"}
        for alias in node.names:
            if is_lifecycle:
                self.aliases[alias.asname or alias.name] = alias.name
            elif is_root_lifecycle and alias.name == "lifecycle":
                self.aliases[alias.asname or alias.name] = "*"

    def visit_Assign(self, node: ast.Assign) -> None:
        point = self._resolve(node.value)
        if point:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.aliases[target.id] = point
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if node.args:
            point = self._resolve(node.args[0])
            if point in self.points:
                method = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", "")
                )
                location = _symbol(self.path, self.stack)
                if method in {
                    "emit",
                    "serial",
                    "waterfall",
                    "dispatch",
                    "dispatch_lifecycle",
                    "_dispatch_lifecycle",
                    "_dispatch",
                    "_notification",
                }:
                    self.producers[point].add(location)
                elif method == "on":
                    self.consumers[point].add(location)
        self.generic_visit(node)

    def _resolve(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if self.aliases.get(node.value.id) == "*":
                return node.attr
        return None


def _scan_sources(
    points: Iterable[str], hook_points: Iterable[str]
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    point_set = set(points)
    producers: dict[str, set[str]] = defaultdict(set)
    consumers: dict[str, set[str]] = defaultdict(set)
    for path in SRC.joinpath("linch").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _SourceVisitor(path, point_set)
        visitor.visit(tree)
        for point, locations in visitor.producers.items():
            producers[point].update(locations)
        for point, locations in visitor.consumers.items():
            consumers[point].update(locations)

    # HookDispatcher resolves the whole hook catalog dynamically through its
    # _METHODS map, so there is no per-point EventBus subscription to discover.
    for point in hook_points:
        consumers[point].add("linch.hooks.dispatcher:HookDispatcher.dispatch")
    return producers, consumers


def _short_payload(point: LifecyclePoint) -> str:
    raw = point._payload_ref
    if point.name == "tools/post-execute":
        return "ToolExecution + result"
    if point.name == "context/service":
        return "Context + key"
    if isinstance(raw, tuple):
        return " + ".join(str(value).rsplit(".", 1)[-1] for value in raw)
    return str(raw).rsplit(".", 1)[-1]


def _cell(values: Iterable[str]) -> str:
    entries = sorted(set(values))
    return "<br>".join(f"`{entry}`" for entry in entries) if entries else "—"


def _generated_block() -> str:
    names, points = _load_points()
    canonical = {names[id(point)]: point for point in points}
    hook_points = {
        name
        for name, point in canonical.items()
        if isinstance(point._payload_ref, str)
        and point._payload_ref.startswith("linch.hooks.contexts.")
    }
    producers, consumers = _scan_sources(canonical, hook_points)
    lines = [
        BEGIN,
        "| Point | Payload | Mode | Producers | Consumers |",
        "|---|---|---|---|---|",
    ]
    for name, point in canonical.items():
        lines.append(
            f"| `{point.name}` | `{_short_payload(point)}` | `{point.mode}` | "
            f"{_cell(producers[name])} | {_cell(consumers[name])} |"
        )
    lines.append(END)
    return "\n".join(lines)


def _replace_block(document: str, block: str) -> str:
    start = document.find(BEGIN)
    finish = document.find(END)
    if start < 0 or finish < start:
        raise ValueError(f"{DOC} is missing lifecycle catalog markers")
    finish += len(END)
    return document[:start] + block + document[finish:]


def main(argv: list[str] | None = None) -> int:
    """Generate or validate the architecture lifecycle catalog.

    Args:
        argv: Optional command-line arguments; defaults to ``sys.argv``.

    Returns:
        Zero when the catalog is current or was written, otherwise one.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the catalog is stale")
    args = parser.parse_args(argv)
    document = DOC.read_text(encoding="utf-8")
    expected = _replace_block(document, _generated_block())
    if args.check:
        if expected != document:
            print(f"{DOC} lifecycle catalog is stale", file=sys.stderr)
            return 1
        return 0
    DOC.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

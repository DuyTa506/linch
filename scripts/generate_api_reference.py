#!/usr/bin/env python3
"""Generate the public API reference from source, without importing Linch."""

from __future__ import annotations

import argparse
import ast
import builtins
import copy
import importlib.util
import inspect
import re
import sys
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DOC = ROOT / "docs/usage/api-reference.md"
USAGE_PAGES = {"linch.ext.scaffold.main": "cli.md"}


def statements(body):
    for node in body:
        if isinstance(node, (ast.If, ast.Try)):
            yield from statements(node.body)  # Canonical try imports precede extra-error stubs.
        else:
            yield node


@lru_cache(None)
def module(name):
    path = SRC.joinpath(*name.split("."))
    path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
    body = list(statements(ast.parse(path.read_text(encoding="utf-8")).body))
    return path, body


def assignment(node):
    return node.targets[0].id if isinstance(node, ast.Assign) else node.target.id


def literal(name, symbol, default=None):
    for node in module(name)[1]:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and assignment(node) == symbol:
            return ast.literal_eval(node.value)
    return default


def resolve(name, symbol):
    if name == "linch":
        exports = {s: (m, s) for m, ss in literal(name, "_EXPORTS", {}).items() for s in ss}
        exports.update(literal(name, "_ALIASES", {}))
        if symbol in exports:
            target, symbol = exports[symbol]
            return resolve(importlib.util.resolve_name(target, name), symbol)
    path, body = module(name)
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    for index, node in enumerate(body):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == symbol:
                    target = importlib.util.resolve_name(
                        "." * node.level + (node.module or ""), package
                    )
                    return resolve(target, alias.name)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == symbol:
                return name, node, ast.get_docstring(node) or ""
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and assignment(node) == symbol:
            following = body[index + 1 : index + 2]
            doc = following[0].value if following and isinstance(following[0], ast.Expr) else None
            return name, node, doc.value if isinstance(doc, ast.Constant) else ""
    raise ValueError(f"Cannot resolve {name}.{symbol}")


def bases(name, node):
    for base in reversed(node.bases):
        base = base.value if isinstance(base, ast.Subscript) else base
        if isinstance(base, ast.Name):
            try:
                yield resolve(name, base.id)[:2]
            except (ValueError, FileNotFoundError):
                pass  # typing/stdlib bases have no project source declaration.


def fields(name, node):
    result = {}
    for owner, base in bases(name, node):
        if isinstance(base, ast.ClassDef):
            result.update(fields(owner, base))
    decorator = next((d for d in node.decorator_list if isinstance(d, ast.Call)), None)
    kw_only = bool(
        decorator
        and any(k.arg == "kw_only" and ast.literal_eval(k.value) for k in decorator.keywords)
    )
    for item in node.body:
        if not isinstance(item, ast.AnnAssign) or "ClassVar" in ast.unparse(item.annotation):
            continue
        annotation, value, keyword = ast.unparse(item.annotation), item.value, kw_only
        if isinstance(value, ast.Call) and ast.unparse(value.func) == "field":
            options = {k.arg: k.value for k in value.keywords}
            if "init" in options and not ast.literal_eval(options["init"]):
                continue
            keyword = ast.literal_eval(options["kw_only"]) if "kw_only" in options else keyword
            value = options.get("default")
            default = (
                "<factory>"
                if "default_factory" in options
                else ast.unparse(value)
                if value
                else None
            )
        else:
            default = ast.unparse(value) if value is not None else None
        result[item.target.id] = (annotation, default, keyword)
    return result


def signature(name, node, public):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = copy.deepcopy(node.args)
        if args.args and args.args[0].arg in {"self", "cls"}:
            args.args.pop(0)
        suffix = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
        return f"{prefix}{public}({ast.unparse(args)}){suffix}"
    if isinstance(node, ast.ClassDef):
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                return signature(name, item, public).removeprefix("def ")
        if any(ast.unparse(d).startswith("dataclass") for d in node.decorator_list):
            parameters, keywords = [], []
            for key, (annotation, default, kw_only) in fields(name, node).items():
                text = f"{key}: {annotation}" + (f" = {default}" if default is not None else "")
                (keywords if kw_only else parameters).append(text)
            return f"{public}({', '.join(parameters + (['*'] + keywords if keywords else []))})"
        if any(ast.unparse(b) == "Protocol" for b in node.bases):
            return "Not instantiable (Protocol); implement its documented methods and attributes."
        for owner, base in bases(name, node):
            if isinstance(base, ast.ClassDef):
                return signature(owner, base, public)
        inherited = ast.unparse(node.bases[0]) if node.bases else "object"
        builtin = StrEnum if inherited == "StrEnum" else getattr(builtins, inherited, None)
        if builtin is None and inherited == "ABC":
            builtin = object  # ABC adds no constructor; its metaclass enforces abstract methods.
        try:
            return (
                f"{public}{inspect.signature(builtin)}"
                if builtin
                else f"Signature unavailable: inherits {inherited}."
            )
        except ValueError:
            return (
                f"Inherited initializer: {inherited}.__init__{inspect.signature(builtin.__init__)}"
            )
    annotation = ast.unparse(node.annotation) if isinstance(node, ast.AnnAssign) else ""
    value = ast.unparse(node.value)
    if isinstance(node.value, ast.Name) and annotation != "TypeAlias":
        owner, target, _ = resolve(name, node.value.id)
        value = ast.unparse(target.value)
    if len(value) > 300:
        value = "...  # value omitted; see source"
    return f"{public}{': ' + annotation if annotation else ''} = {value}"


def generate():
    modules = ["linch"] + [
        ".".join(p.parent.relative_to(SRC).parts)
        for p in sorted(SRC.joinpath("linch/ext").rglob("__init__.py"))
    ]
    pages = [
        (p, p.read_text(encoding="utf-8"))
        for p in sorted(DOC.parent.glob("*.md"))
        if p.name not in {DOC.name, "README.md"}
    ]
    lines = [
        "# API reference",
        "",
        "Use this reference to find supported imports and source signatures.",
        "",
        "Generated by `python scripts/generate_api_reference.py`; validate with `--check`.",
        "",
        "Annotations/defaults retain source spelling; `<factory>` is a fresh dataclass default.",
        "Constants and type aliases show declarations in place of constructor signatures.",
        "",
    ]
    missing = []
    for public_module in modules:
        for public in literal(public_module, "__all__", []):
            owner, node, doc = resolve(public_module, public)
            if not doc:
                missing.append(f"{public_module}.{public} ({owner}:{node.lineno})")
            kind = "Class" if isinstance(node, ast.ClassDef) else "Function"
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                alias = (
                    isinstance(node, ast.AnnAssign) and ast.unparse(node.annotation) == "TypeAlias"
                )
                alias = alias or ast.unparse(node.value).startswith("Literal[")
                alias = alias or (
                    isinstance(node.value, ast.BinOp) and isinstance(node.value.op, ast.BitOr)
                )
                kind = "Type alias" if alias else "Constant (not callable)"
            lines += [
                f"### `{public_module}.{public}`\n",
                f"Kind: {kind}.\n",
                f"```python\nfrom {public_module} import {public}\n```\n",
                f"```text\n{signature(owner, node, public)}\n```\n",
                (doc.splitlines()[0] if doc else "") + "\n",
            ]
            override = USAGE_PAGES.get(f"{public_module}.{public}")
            usage = next(
                (
                    p
                    for p, text in pages
                    if (
                        p.name == override
                        if override
                        else re.search(rf"(?<![\w]){re.escape(public)}(?![\w])", text)
                    )
                ),
                None,
            )
            if usage:
                lines += [f"Usage: [{usage.stem}]({usage.name}).", ""]
    return "\n".join(lines), missing


def main(argv=None):
    """Write the reference, or return nonzero for stale output or missing documentation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the reference is stale")
    args = parser.parse_args(argv)
    expected, missing = generate()
    if missing:
        print("Missing docstrings:\n" + "\n".join(missing), file=sys.stderr)
        return 1
    if args.check:
        if not DOC.exists() or DOC.read_text(encoding="utf-8") != expected:
            print(f"{DOC} API reference is stale", file=sys.stderr)
            return 1
    else:
        DOC.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Small deterministic serializers used by generated artifacts."""

from __future__ import annotations

import ast
import json
import math
from typing import Any

_PYTHON_LITERAL_WIDTH = 88


def canonical_json(value: Any, *, indent: int | None = None) -> str:
    """Serialize JSON with stable key order and no non-finite values."""

    separators = (",", ":") if indent is None else None
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        indent=indent,
        separators=separators,
        sort_keys=True,
    )


def python_literal(value: Any) -> str:
    """Return a deterministic Python literal and verify that it parses."""

    _reject_nonfinite(value)
    rendered = _render_python_literal(value, indent=0)
    ast.literal_eval(rendered)
    return rendered


def toml_string(value: str) -> str:
    """Render a TOML basic string using JSON's compatible escaping rules."""

    return json.dumps(value, ensure_ascii=False)


def normalized_text(value: str) -> str:
    """Normalize generated text to LF with exactly one trailing newline."""

    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers cannot be serialized")
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_nonfinite(key)
            _reject_nonfinite(child)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            _reject_nonfinite(child)


def _render_python_literal(value: Any, *, indent: int) -> str:
    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, str):
        # JSON basic strings are valid Python string literals and consistently
        # use the double-quote style selected by Ruff's formatter.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: repr(item[0]))
        inline_parts = [
            f"{_render_python_literal(key, indent=indent)}: "
            f"{_render_python_literal(child, indent=indent)}"
            for key, child in items
        ]
        inline = "{" + ", ".join(inline_parts) + "}"
        if "\n" not in inline and indent + len(inline) <= _PYTHON_LITERAL_WIDTH:
            return inline
        lines = ["{"]
        for key, child in items:
            rendered_key = _render_python_literal(key, indent=indent + 4)
            rendered_child = _render_python_literal(child, indent=indent + 4)
            lines.append(" " * (indent + 4) + f"{rendered_key}: {rendered_child},")
        lines.append(" " * indent + "}")
        return "\n".join(lines)
    if isinstance(value, (list, tuple)):
        rendered_items = [_render_python_literal(child, indent=indent + 4) for child in value]
        if isinstance(value, tuple):
            if not rendered_items:
                return "()"
            suffix = "," if len(rendered_items) == 1 else ""
            inline = "(" + ", ".join(rendered_items) + suffix + ")"
            opening, closing = "(", ")"
        else:
            inline = "[" + ", ".join(rendered_items) + "]"
            opening, closing = "[", "]"
        if "\n" not in inline and indent + len(inline) <= _PYTHON_LITERAL_WIDTH:
            return inline
        lines = [opening]
        lines.extend(" " * (indent + 4) + item + "," for item in rendered_items)
        lines.append(" " * indent + closing)
        return "\n".join(lines)
    if isinstance(value, set):
        ordered = sorted(value, key=repr)
        if not ordered:
            return "set()"
        rendered_items = [_render_python_literal(child, indent=indent + 4) for child in ordered]
        inline = "{" + ", ".join(rendered_items) + "}"
        if "\n" not in inline and indent + len(inline) <= _PYTHON_LITERAL_WIDTH:
            return inline
        return (
            "{\n"
            + "\n".join(" " * (indent + 4) + item + "," for item in rendered_items)
            + "\n"
            + " " * indent
            + "}"
        )
    raise TypeError(f"unsupported Python literal type: {type(value)!r}")


__all__ = ["canonical_json", "normalized_text", "python_literal", "toml_string"]

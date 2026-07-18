"""Dump the live OpenAPI document and capability catalog for the frontend.

These are the only supported sources of ``web/openapi.json`` and the catalog test
fixture. Hand-editing either lets the TypeScript client drift away from the real
server, which is exactly how the previous frontend ended up calling routes that
did not exist and inventing capability badges.

Usage:
    python scripts/dump_openapi.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from linch_studio.catalog import catalog_document
from linch_studio.server.app import create_app

WEB = Path(__file__).resolve().parents[1] / "web"
DEFAULT_OUT = WEB / "openapi.json"
CATALOG_OUT = WEB / "src" / "model" / "__fixtures__" / "catalog.json"


def openapi_document() -> dict[str, object]:
    """Build the OpenAPI schema against a throwaway workspace."""

    with tempfile.TemporaryDirectory() as workspace:
        return create_app(workspace).openapi()


def _render(document: object) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def render() -> str:
    return _render(openapi_document())


def _targets() -> list[tuple[Path, str]]:
    return [(DEFAULT_OUT, render()), (CATALOG_OUT, _render(catalog_document()))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if a file on disk is stale instead of rewriting it.",
    )
    args = parser.parse_args(argv)

    stale = False
    for path, document in _targets():
        if args.check:
            current = path.read_text(encoding="utf-8") if path.is_file() else ""
            if current != document:
                print(f"{path} is stale; run: python scripts/dump_openapi.py", file=sys.stderr)
                stale = True
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document, encoding="utf-8")
        print(f"wrote {path}")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Refresh the committed SDK docs and audited examples used by Studio support.

Run from linch_ui/ after any change to the parent repo's docs:

    python scripts/sync_knowledge.py

The unit tests for the docs and examples snapshots fail until the committed
corpus matches the parent repository again.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from linch_studio.authoring.knowledge import build_toc, collect_example_docs, collect_sdk_docs

LINCH_UI_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = LINCH_UI_ROOT.parent / "docs"
EXAMPLES_ROOT = LINCH_UI_ROOT.parent / "examples"
SNAPSHOT_ROOT = LINCH_UI_ROOT / "src" / "linch_studio" / "authoring" / "knowledge"


def main() -> int:
    if not DOCS_ROOT.is_dir() or not EXAMPLES_ROOT.is_dir():
        print("SDK docs or examples tree was not found", file=sys.stderr)
        return 1
    files = collect_sdk_docs(DOCS_ROOT)
    examples = collect_example_docs(EXAMPLES_ROOT)
    sdk_root = SNAPSHOT_ROOT / "sdk"
    if sdk_root.exists():
        shutil.rmtree(sdk_root)
    for relative, text in sorted(files.items()):
        target = sdk_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    examples_root = SNAPSHOT_ROOT / "examples"
    if examples_root.exists():
        shutil.rmtree(examples_root)
    for relative, text in sorted(examples.items()):
        target = examples_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    toc = json.dumps(build_toc(files), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    (SNAPSHOT_ROOT / "toc.json").write_text(toc + "\n", encoding="utf-8")
    print(
        f"synced {len(files)} docs and {len(examples)} examples into "
        f"{SNAPSHOT_ROOT.relative_to(LINCH_UI_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

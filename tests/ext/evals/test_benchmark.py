from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("alias", ["text", "contains"])
def test_load_eval_suite_rejects_text_contains_legacy_field_alias(
    tmp_path: Path, alias: str
) -> None:
    from linch.ext.evals import load_eval_suite

    suite_path = tmp_path / "suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "cases": [{"prompt": "Say ok", "expected": "ok"}],
                "scorers": [{"type": "text_contains", alias: "ok"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="substring"):
        load_eval_suite(suite_path)

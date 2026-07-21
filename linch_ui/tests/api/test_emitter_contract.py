"""Cross-language guard for the frontend's Blueprint -> YAML emitter.

The web canvas edits a Blueprint and PUTs re-serialized YAML. That emitter lives
in TypeScript, so nothing in Python would otherwise notice if it drifted away
from the strict spec. These tests read fixtures emitted by
`web/scripts/emit_fixtures.ts` (`npm run fixtures:emit`) and assert the emitted
YAML parses to the *same canonical digest* as the JSON it came from.

If this fails, regenerate the fixtures and reconcile the emitter — do not relax
the assertion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from linch_studio.spec import blueprint_digest, load_blueprint

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "web" / "src" / "model" / "__fixtures__"


def _fixture_names() -> list[str]:
    if not FIXTURE_DIR.is_dir():
        return []
    return sorted(path.stem for path in FIXTURE_DIR.glob("*.yaml"))


FIXTURES = _fixture_names()

pytestmark = pytest.mark.skipif(
    not FIXTURES,
    reason="frontend fixtures absent; run: cd web && npm run fixtures:emit",
)


@pytest.mark.parametrize("name", FIXTURES)
def test_emitted_yaml_is_structurally_valid(name: str) -> None:
    source = (FIXTURE_DIR / f"{name}.yaml").read_text(encoding="utf-8")

    result = load_blueprint(source)

    assert result.blueprint is not None, [item.message for item in result.diagnostics]


@pytest.mark.parametrize("name", FIXTURES)
def test_emitted_yaml_matches_source_digest(name: str) -> None:
    """Pruning nulls for readability must not move the canonical digest."""

    source = (FIXTURE_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    data = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))

    from_yaml = load_blueprint(source).blueprint
    from_json = load_blueprint(json.dumps(data)).blueprint

    assert from_yaml is not None and from_json is not None
    assert blueprint_digest(from_yaml) == blueprint_digest(from_json)

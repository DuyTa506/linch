from __future__ import annotations

import re
from pathlib import Path

import pytest

import linch
from linch.ext.scaffold import main
from linch.skills.loader import load_skills_from_dir

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "src/linch/_skills/build-agent-with-linch"


def test_skill_loads_through_linch(tmp_path) -> None:
    assert main(["skill", "install", "--target", "linch", "--dir", str(tmp_path)]) == 0
    skills, skipped = load_skills_from_dir(str(tmp_path / ".linch"), set())
    assert not skipped
    assert [skill.name for skill in skills] == ["build-agent-with-linch"]


def test_skill_references_exist_and_stay_shipped() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    paths = set(re.findall(r"`(docs/[\w./-]+\.md|examples/[\w./-]+\.py)`", text))
    assert len(paths) >= 20
    assert not [path for path in paths if not (ROOT / path).is_file()]


def test_skill_names_are_public() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    names = {"empty_tools", "tool", "ContextInjectionHook"}
    assert all(f"`{name}" in text or name in text for name in names)
    assert names <= set(linch.__all__)


def test_install_defaults_to_claude_and_codex_and_guards_overwrite(tmp_path, capsys) -> None:
    assert main(["skill", "install", "--dir", str(tmp_path)]) == 0
    claude = tmp_path / ".claude/skills/build-agent-with-linch/SKILL.md"
    assert claude.is_file()
    assert (tmp_path / ".agents/skills/build-agent-with-linch/SKILL.md").is_file()
    assert not (tmp_path / ".linch").exists()
    claude.write_text("old", encoding="utf-8")

    assert main(["skill", "install", "--dir", str(tmp_path)]) == 1
    assert claude.read_text(encoding="utf-8") == "old"
    assert "--force" in capsys.readouterr().err

    assert main(["skill", "install", "--dir", str(tmp_path), "--force"]) == 0
    assert claude.read_text(encoding="utf-8") != "old"


@pytest.mark.parametrize("target", [".claude/skills", ".agents/skills"])
def test_new_project_includes_skill(tmp_path, target) -> None:
    assert main(["new", "demo-agent", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / "demo-agent" / target / "build-agent-with-linch/SKILL.md").is_file()

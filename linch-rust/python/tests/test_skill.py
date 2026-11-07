"""`linch-rust skill install`, and the `build-agent-with-linch-rust` skill it copies."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from scripted import config, text_turn, tool_turn

import linch_rust
from linch_rust import Server, SkillsLoadedEvent, ToolCallEndEvent
from linch_rust.cli import _scaffold, main

PYTHON_DIR = Path(__file__).resolve().parents[1]
WORKSPACE = PYTHON_DIR.parent
NAME = "build-agent-with-linch-rust"
SKILL = WORKSPACE / "skills" / NAME
CLAUDE = Path(".claude/skills") / NAME
CODEX = Path(".agents/skills") / NAME
LINCH = Path(".linch/skills") / NAME


def installed(root: Path, target: Path) -> bool:
    return (root / target / "SKILL.md").is_file() and (
        root / target / "references" / "design.md"
    ).is_file()


@pytest.mark.source
def test_install_defaults_to_claude_and_codex(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["skill", "install", "--dir", str(tmp_path)]) == 0
    assert installed(tmp_path, CLAUDE) and installed(tmp_path, CODEX)
    assert not (tmp_path / ".linch").exists()
    assert (tmp_path / CLAUDE / "SKILL.md").read_bytes() == (SKILL / "SKILL.md").read_bytes()
    out = capsys.readouterr().out
    assert f"created {tmp_path / CLAUDE}\n" in out and f"created {tmp_path / CODEX}\n" in out


def test_install_refuses_an_installed_copy_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["skill", "install", "--target", "claude", "--dir", str(tmp_path)]) == 0
    skill = tmp_path / CLAUDE / "SKILL.md"
    skill.write_text("edited", encoding="utf-8")

    assert main(["skill", "install", "--dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert f"{tmp_path / CLAUDE} already exists; pass --force to replace it" in err
    assert skill.read_text(encoding="utf-8") == "edited"
    assert not (tmp_path / ".agents").exists(), "nothing is copied when one target is in the way"


@pytest.mark.source
def test_force_replaces_an_installed_copy(tmp_path: Path) -> None:
    assert main(["skill", "install", "--dir", str(tmp_path)]) == 0
    (tmp_path / CLAUDE / "SKILL.md").write_text("old", encoding="utf-8")
    (tmp_path / CLAUDE / "stale.md").write_text("from an older version", encoding="utf-8")

    assert main(["skill", "install", "--dir", str(tmp_path), "--force"]) == 0
    assert (tmp_path / CLAUDE / "SKILL.md").read_bytes() == (SKILL / "SKILL.md").read_bytes()
    assert not (tmp_path / CLAUDE / "stale.md").exists()
    assert installed(tmp_path, CODEX)


@pytest.mark.parametrize(
    ("targets", "expected"),
    [
        (["linch"], [LINCH]),
        (["codex"], [CODEX]),
        (["claude", "linch"], [CLAUDE, LINCH]),
        (["linch", "linch"], [LINCH]),
    ],
)
def test_target_picks_the_coding_agents(
    tmp_path: Path, targets: list[str], expected: list[Path]
) -> None:
    argv = ["skill", "install", "--dir", str(tmp_path)]
    for target in targets:
        argv += ["--target", target]
    assert main(argv) == 0
    for target in (CLAUDE, CODEX, LINCH):
        assert installed(tmp_path, target) == (target in expected)


def test_an_unknown_target_is_a_usage_error(tmp_path: Path) -> None:
    assert main(["skill", "install", "--target", "cursor", "--dir", str(tmp_path)]) == 2
    assert not any(tmp_path.iterdir())


def test_install_defaults_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["skill", "install"]) == 0
    assert installed(tmp_path, CLAUDE) and installed(tmp_path, CODEX)
    assert f"created {CLAUDE}\n" in capsys.readouterr().out


def test_new_installs_the_skill_for_claude_and_codex(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "demo", "--dir", str(tmp_path)]) == 0
    project = tmp_path / "demo"
    assert installed(project, CLAUDE) and installed(project, CODEX)
    assert not (project / ".linch").exists()
    out = capsys.readouterr().out
    assert f"created {project / CLAUDE}\n" in out and f"created {project / CODEX}\n" in out


def test_install_fails_without_the_packaged_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_scaffold, "skill_roots", lambda: [tmp_path / "nowhere"])
    assert main(["skill", "install", "--dir", str(tmp_path)]) == 1
    assert "skill not found" in capsys.readouterr().err
    assert not (tmp_path / ".claude").exists()


@pytest.mark.source
def test_the_wheel_ships_the_skill() -> None:
    pyproject = tomllib.loads((PYTHON_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    include = pyproject["tool"]["maturin"]["include"]
    for format in ("sdist", "wheel"):
        assert {"path": "linch_rust/_skills/**/*", "format": format} in include
    packaged = _scaffold.skill_roots()[0]
    assert packaged == PYTHON_DIR / "src" / "linch_rust" / "_skills"
    assert packaged.resolve() == SKILL.parent.resolve()
    assert (packaged / NAME / "SKILL.md").is_file()


async def test_the_skill_loads_in_a_linch_rust_agent(tmp_path: Path) -> None:
    assert main(["skill", "install", "--target", "linch", "--dir", str(tmp_path)]) == 0
    turns = [tool_turn("c1", "Skill", {"skill": NAME}), text_turn("Read it.")]
    agent_config = config(turns, skills=True, config_dir=str(tmp_path / ".linch"))
    async with Server() as server:
        agent = await server.open_agent(agent_config)
        assert agent.skipped == []
        loaded: list[str] = []
        body = ""
        async for envelope in await agent.run("s1", "Build an agent."):
            event = envelope.event
            if isinstance(event, SkillsLoadedEvent):
                loaded = [skill["name"] for skill in event.skills]
            elif isinstance(event, ToolCallEndEvent):
                assert not event.is_error, event.result
                body = event.result["content"][0]["text"]
    assert NAME in loaded
    assert "linch-rust docs path" in body


DOC_PATH = re.compile(r"`((?:usage|architecture)/[\w-]+\.md)(?:#[\w-]+)?`")


@pytest.mark.source
@pytest.mark.parametrize("page", ["SKILL.md", "references/design.md"])
def test_every_doc_path_the_skill_names_ships(page: str) -> None:
    text = (SKILL / page).read_text(encoding="utf-8")
    paths = set(DOC_PATH.findall(text))
    assert len(paths) >= 8
    assert sorted(path for path in paths if not (WORKSPACE / "docs" / path).is_file()) == []


@pytest.mark.source
def test_the_names_the_skill_teaches_are_public() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    names = {
        "Server",
        "AgentConfig",
        "Allow",
        "Deny",
        "ToolContext",
        "tool",
        "callback",
        "ResultEvent",
        "SuspendedEvent",
        "ContractMismatch",
        "SessionBusy",
    }
    taught = set(re.findall(r"`@?([A-Za-z_]\w*)", text))
    assert names <= taught
    assert names <= set(linch_rust.__all__)

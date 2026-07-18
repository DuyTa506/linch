from __future__ import annotations

import json
from pathlib import Path

from linch_studio.cli import main
from linch_studio.spec import API_VERSION, load_blueprint_file

GOLDEN = Path(__file__).parents[1] / "golden" / "standard_agent.yaml"


def test_new_creates_draft_and_refuses_to_overwrite(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "designs"

    assert main(["new", "nightly-review", "--workspace", str(workspace)]) == 0
    project = workspace / "nightly_review"
    assert (project / "linch-studio.yaml").is_file()
    layout = json.loads((project / ".linch-studio" / "layout.json").read_text())
    assert layout == {"nodes": [], "viewport": {"x": 0.0, "y": 0.0, "zoom": 1.0}}
    assert main(["new", "nightly-review", "--workspace", str(workspace)]) == 1
    assert "already exists" in capsys.readouterr().err


def test_new_uses_the_shared_template_registry(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "designs"

    assert (
        main(
            [
                "new",
                "verified-agent",
                "--workspace",
                str(workspace),
                "--template",
                "goal_verified",
            ]
        )
        == 0
    )
    capsys.readouterr()
    result = load_blueprint_file(workspace / "verified_agent" / "linch-studio.yaml")

    assert result.blueprint is not None
    assert result.blueprint.spec.runtime.agent.completion.mode == "verifier_gated"
    assert result.blueprint.spec.runtime.agent.completion.verifiers[0].kind == "custom_todo"


def test_migrate_requires_worker_limit_ack_and_refuses_overwrite(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "legacy.yaml"
    output = tmp_path / "migrated.yaml"
    source.write_text(
        """\
apiVersion: studio.linch.dev/v1alpha1
kind: LinchProject
metadata:
  name: demo
  title: Demo
spec:
  package: demo
  provider:
    kind: openai_responses
    model: gpt-5
  primaryAgent:
    maxTurns: 8
    budget:
      maxTokens: 20000
  subagents:
    - id: worker
      displayName: Worker
      instructions: Do one task.
      maxTurns: 2
""",
        encoding="utf-8",
    )

    assert main(["migrate", str(source), "--out", str(output)]) == 2
    assert not output.exists()
    assert "--accept-dropped-worker-limits" in capsys.readouterr().err

    assert (
        main(
            [
                "migrate",
                str(source),
                "--out",
                str(output),
                "--accept-dropped-worker-limits",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert load_blueprint_file(output).blueprint is not None
    assert f"apiVersion: {API_VERSION}" in output.read_text(encoding="utf-8")
    assert f"apiVersion: {API_VERSION}" not in source.read_text(encoding="utf-8")

    assert (
        main(
            [
                "migrate",
                str(source),
                "--out",
                str(output),
                "--accept-dropped-worker-limits",
            ]
        )
        == 1
    )
    assert "refusing to overwrite" in capsys.readouterr().err


def test_validate_preview_schema_and_export_commands(tmp_path: Path, capsys) -> None:
    assert main(["validate", str(GOLDEN), "--json"]) == 0
    diagnostics = json.loads(capsys.readouterr().out)
    assert [item["code"] for item in diagnostics] == ["schema.migrated"]
    assert diagnostics[0]["severity"] == "info"

    assert main(["preview", str(GOLDEN), "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert {item["path"] for item in preview} >= {
        "pyproject.toml",
        "DEVELOPMENT.md",
        ".linch-studio-manifest.json",
    }
    assert all("content" not in item for item in preview)

    schema = tmp_path / "schema" / "studio.schema.json"
    assert main(["schema", "--out", str(schema)]) == 0
    capsys.readouterr()
    assert json.loads(schema.read_text(encoding="utf-8"))["title"] == "Blueprint"

    output = tmp_path / "generated"
    assert main(["export", str(GOLDEN), "--out", str(output)]) == 0
    capsys.readouterr()
    assert (output / "src" / "support_assistant" / "agent.py").is_file()
    assert main(["export", str(GOLDEN), "--out", str(output)]) == 1
    assert "not empty" in capsys.readouterr().err


def test_invalid_cli_name_and_port_are_usage_errors(tmp_path: Path, capsys) -> None:
    assert main(["new", "Not Valid", "--workspace", str(tmp_path)]) == 2
    assert "project name" in capsys.readouterr().err
    assert main(["serve", "--workspace", str(tmp_path), "--port", "0"]) == 2
    assert "port" in capsys.readouterr().err


def test_serve_rejects_partial_ai_authoring_configuration(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LINCH_STUDIO_PROVIDER", "openai_responses")
    monkeypatch.delenv("LINCH_STUDIO_MODEL", raising=False)

    assert main(["serve", "--workspace", str(tmp_path)]) == 1
    assert "invalid AI authoring configuration" in capsys.readouterr().err

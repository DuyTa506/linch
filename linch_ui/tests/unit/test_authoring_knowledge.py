"""Knowledge substrate for the authoring agent: snapshot, ToC, search, and read tools."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from linch_studio.authoring.knowledge import (
    SEARCH_LIMIT,
    SECTION_CHARS,
    SNIPPET_CHARS,
    KnowledgeBase,
    UnknownAnchorError,
    build_toc,
    collect_example_docs,
    collect_sdk_docs,
    parse_sections,
    slugify,
)
from linch_studio.authoring.tools import AuthoringDeps, ReadSectionTool, SearchDocsTool
from linch_studio.catalog.registry import CATALOG

LINCH_UI_ROOT = Path(__file__).parents[2]
SDK_DOCS_ROOT = LINCH_UI_ROOT.parent / "docs"
EXAMPLES_ROOT = LINCH_UI_ROOT.parent / "examples"
SNAPSHOT_ROOT = LINCH_UI_ROOT / "src" / "linch_studio" / "authoring" / "knowledge"


def write_snapshot(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        target = root / "sdk" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    toc = build_toc(files)
    (root / "toc.json").write_text(
        json.dumps(toc, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


@dataclass
class FakeContext:
    deps: object


def test_slugify_strips_backticks_punctuation_and_case() -> None:
    assert slugify("The `wf` context") == "the-wf-context"
    assert slugify("Dependencies (shared app state)") == "dependencies-shared-app-state"
    assert slugify("  Retry  ") == "retry"


def test_parse_sections_ignores_hash_lines_inside_code_fences() -> None:
    text = (
        "# Tools\n"
        "\n"
        "intro\n"
        "\n"
        "```python\n"
        "# No built-in tools (pure domain agent)\n"
        "x = 1\n"
        "```\n"
        "\n"
        "## Retry\n"
        "body\n"
    )
    sections = parse_sections(text)
    assert [item.slug for item in sections] == ["tools", "retry"]
    # The fenced comment stays inside the first section's body.
    assert sections[0].start_line == 1
    assert sections[0].end_line == 9
    assert sections[1].start_line == 10


def test_parse_sections_disambiguates_duplicate_headings_within_one_file() -> None:
    text = "# Doc\n\n## Setup\na\n\n## Setup\nb\n"
    sections = parse_sections(text)
    assert [item.slug for item in sections] == ["doc", "setup", "setup-2"]


def test_build_toc_is_deterministic_sorted_and_titled() -> None:
    files = {
        "usage/b.md": "# Beta\n\n## One\n",
        "usage/a.md": "# Alpha doc\n\nbody\n",
    }
    first = build_toc(files)
    second = build_toc(dict(reversed(files.items())))
    assert first == second
    assert [entry["path"] for entry in first["files"]] == ["usage/a.md", "usage/b.md"]
    assert first["files"][0]["title"] == "Alpha doc"
    assert first["files"][1]["sections"][1]["slug"] == "one"


def test_search_ranks_heading_matches_first_and_bounds_output(tmp_path: Path) -> None:
    files = {
        "usage/a.md": "# Alpha\n\nzebra appears in prose only\n\n## Zebra habits\n\nbody\n",
        "usage/b.md": "# Beta\n\nnothing here\n",
    }
    write_snapshot(tmp_path, files)
    kb = KnowledgeBase(root=tmp_path)

    hits = kb.search("zebra")
    assert hits[0].anchor == "usage/a.md#zebra-habits"
    assert all(len(hit.snippet) <= SNIPPET_CHARS for hit in hits)
    assert "usage/b.md" not in {hit.anchor.split("#")[0] for hit in hits}


def test_search_caps_the_number_of_hits(tmp_path: Path) -> None:
    body = "\n".join(f"## Part {i}\n\nzebra fact {i}\n" for i in range(SEARCH_LIMIT + 4))
    write_snapshot(tmp_path, {"usage/a.md": f"# Alpha\n\n{body}\n"})
    kb = KnowledgeBase(root=tmp_path)

    assert len(kb.search("zebra")) == SEARCH_LIMIT


def test_vietnamese_schedule_query_uses_unicode_terms_and_documented_aliases() -> None:
    """Accented Vietnamese must not degrade into one-letter provider false positives."""

    kb = KnowledgeBase()
    hits = kb.search("lập lịch agent có loop cron và multi-agent")
    anchors = [item.anchor for item in hits]
    assert anchors
    assert not anchors[0].startswith("usage/providers.md")
    assert any("schedul" in anchor or "loop" in anchor for anchor in anchors)


def test_read_section_returns_bounded_body_and_flags_truncation(tmp_path: Path) -> None:
    long_body = "word " * 3000
    write_snapshot(tmp_path, {"usage/a.md": f"# Alpha\n\n## Big\n\n{long_body}\n"})
    kb = KnowledgeBase(root=tmp_path)

    section = kb.section("usage/a.md#big")
    assert section.truncated is True
    assert len(section.text) <= SECTION_CHARS

    intro = kb.section("usage/a.md#alpha")
    assert intro.truncated is False
    assert intro.text.startswith("# Alpha")


def test_read_section_unknown_anchor_suggests_near_misses(tmp_path: Path) -> None:
    write_snapshot(tmp_path, {"usage/a.md": "# Alpha\n\n## Retry\n\nbody\n"})
    kb = KnowledgeBase(root=tmp_path)

    with pytest.raises(UnknownAnchorError) as failure:
        kb.section("usage/a.md#retrry")
    assert "usage/a.md#retry" in failure.value.suggestions


def test_catalog_doc_is_generated_from_the_live_catalog(tmp_path: Path) -> None:
    write_snapshot(tmp_path, {"usage/a.md": "# Alpha\n"})
    kb = KnowledgeBase(root=tmp_path)

    first_capability = CATALOG.capabilities[0]
    area_text = kb.section(f"catalog/capabilities.md#{first_capability.area.id}").text
    assert first_capability.id in area_text
    assert first_capability.status.badge in area_text

    matrix_text = kb.section("catalog/capabilities.md#relation-matrix").text
    first_relation = CATALOG.relation_matrix.relations[0]
    assert first_relation.id in matrix_text

    assert kb.search(first_capability.id)[0].anchor.startswith("catalog/capabilities.md#")


def test_catalog_doc_says_capability_ids_are_not_wire_kinds(tmp_path: Path) -> None:
    """Capability IDs like `function_skeleton` must never be read as YAML kinds.

    Live regression: the design model copied a catalog capability ID into a
    tool's `kind` field and failed the discriminated union three times over.
    """
    write_snapshot(tmp_path, {"usage/a.md": "# Alpha\n"})
    kb = KnowledgeBase(root=tmp_path)

    intro = kb.section(
        "catalog/capabilities.md#capability-catalog-generated-from-the-live-registry"
    ).text
    assert "never" in intro
    assert "`kind`" in intro
    assert "function" in intro and "class" in intro and "database" in intro


def test_blueprint_reference_doc_shows_complete_spec_examples(tmp_path: Path) -> None:
    write_snapshot(tmp_path, {"usage/a.md": "# Alpha\n"})
    kb = KnowledgeBase(root=tmp_path)

    directed = kb.section("studio/blueprint.md#directed-workflow-example").text
    assert "apiVersion: studio.linch.dev/v1alpha2" in directed
    assert "kind: directed" in directed
    assert not kb.section("studio/blueprint.md#directed-workflow-example").truncated

    routine = kb.section("studio/blueprint.md#routine-and-cron-trigger-example").text
    assert "cron" in routine
    assert "routines:" in routine

    goal = kb.section("studio/blueprint.md#goal-verified-completion-example").text
    assert "verifier" in goal.lower()

    # Live regression: with no worked fan-out/fan-in example to copy, a build
    # turn for "three parallel specialist subagents merging into one report"
    # burned hundreds of thousands of tokens re-deriving the shape from prose
    # docs alone and repeatedly crashed. A concrete example must be present so
    # the model can copy it instead of re-deriving it from scratch.
    fan_out_section = kb.section("studio/blueprint.md#fan-out-fan-in-workflow-example")
    fan_out = fan_out_section.text
    assert not fan_out_section.truncated
    assert fan_out.count("dependsOn:") >= 4
    assert "subagent:" in fan_out
    assert "subagents:" in fan_out
    # Live regression: with no workflow_run routine example anywhere in the
    # knowledge base, the model guessed plausible-but-wrong field names
    # (`workflow`, `description`) for wiring a CI trigger to a directed
    # workflow, instead of the real field: `target`.
    assert "kind: workflow_run" in fan_out
    assert "kind: ci" in fan_out
    assert "target: review_workflow" in fan_out

    # The doc is the top search hit when the model asks for the spec shape.
    hits = kb.search("LinchProject spec workflow trigger routine yaml schema")
    assert any(hit.anchor.startswith("studio/blueprint.md#") for hit in hits[:3])


def test_toc_prompt_is_compact_and_lists_every_file(tmp_path: Path) -> None:
    write_snapshot(
        tmp_path,
        {"usage/a.md": "# Alpha\n\n## One\n", "architecture/b.md": "# Beta\n"},
    )
    kb = KnowledgeBase(root=tmp_path)

    prompt = kb.toc_prompt()
    assert "usage/a.md" in prompt
    assert "architecture/b.md" in prompt
    assert "catalog/capabilities.md" in prompt
    assert "studio/blueprint.md" in prompt
    assert len(prompt) <= 3500


def test_tools_are_read_scoped_bounded_and_reach_the_kb_through_deps(tmp_path: Path) -> None:
    write_snapshot(tmp_path, {"usage/a.md": "# Alpha\n\n## Retry\n\nzebra body\n"})
    deps = AuthoringDeps(knowledge=KnowledgeBase(root=tmp_path))
    search = SearchDocsTool()
    read = ReadSectionTool()

    for tool in (search, read):
        assert tool.scope == "read"
        assert tool.parallel is True
        assert tool.input_schema["additionalProperties"] is False

    found = asyncio.run(search.execute({"query": "zebra"}, FakeContext(deps=deps)))
    assert found.is_error is False
    assert "usage/a.md#retry" in found.content

    body = asyncio.run(read.execute({"anchor": "usage/a.md#retry"}, FakeContext(deps=deps)))
    assert body.is_error is False
    assert "zebra body" in body.content

    missing = asyncio.run(read.execute({"anchor": "usage/a.md#nope"}, FakeContext(deps=deps)))
    assert missing.is_error is True
    assert "usage/a.md#retry" in missing.content

    no_deps = asyncio.run(search.execute({"query": "zebra"}, FakeContext(deps=None)))
    assert no_deps.is_error is True


def test_tool_results_carry_a_bounded_one_line_summary_for_display(tmp_path: Path) -> None:
    # ToolResult.summary is otherwise unused by these tools; the chat UI's
    # compact tool-call row (Claude-Code-style: one line, expand for full)
    # reads it as the post-call result line, distinct from the pre-call
    # summarize() line ("search_docs: <query>").
    write_snapshot(tmp_path, {"usage/a.md": "# Alpha\n\n## Retry\n\nzebra body\n"})
    deps = AuthoringDeps(knowledge=KnowledgeBase(root=tmp_path))
    search = SearchDocsTool()
    read = ReadSectionTool()

    found = asyncio.run(search.execute({"query": "zebra"}, FakeContext(deps=deps)))
    assert found.summary == "1 result"

    # The live catalog doc is always in the index too (see KnowledgeBase), so
    # this must be a token that cannot overlap with catalog text either.
    no_hits = asyncio.run(search.execute({"query": "zzqxvbnotfound12345"}, FakeContext(deps=deps)))
    assert no_hits.summary == "no matches"

    body = asyncio.run(read.execute({"anchor": "usage/a.md#retry"}, FakeContext(deps=deps)))
    assert body.summary == f"{len(body.content)} chars"

    missing = asyncio.run(read.execute({"anchor": "usage/a.md#nope"}, FakeContext(deps=deps)))
    assert missing.summary == "unknown anchor"


def test_tool_validate_rejects_blank_and_oversized_input() -> None:
    search = SearchDocsTool()
    read = ReadSectionTool()

    assert search.validate({"query": " zebra "}) == {"query": "zebra"}
    with pytest.raises(ValueError):
        search.validate({"query": "   "})
    with pytest.raises(ValueError):
        search.validate({"query": "x" * 500})
    with pytest.raises(ValueError):
        read.validate({"anchor": ""})
    with pytest.raises(ValueError):
        read.validate({})


def test_committed_snapshot_matches_the_parent_sdk_docs() -> None:
    if not SDK_DOCS_ROOT.is_dir():
        pytest.skip("SDK docs tree is not present (installed wheel)")

    expected = collect_sdk_docs(SDK_DOCS_ROOT)
    committed_root = SNAPSHOT_ROOT / "sdk"
    committed = {
        str(path.relative_to(committed_root)): path.read_text(encoding="utf-8")
        for path in sorted(committed_root.rglob("*.md"))
    }
    assert committed == expected, "snapshot is stale — run: python scripts/sync_knowledge.py"

    committed_toc = json.loads((SNAPSHOT_ROOT / "toc.json").read_text(encoding="utf-8"))
    assert committed_toc == build_toc(expected), (
        "toc.json is stale — run: python scripts/sync_knowledge.py"
    )


def test_committed_examples_snapshot_matches_the_parent_examples() -> None:
    if not EXAMPLES_ROOT.is_dir():
        pytest.skip("SDK examples tree is not present (installed wheel)")

    expected = collect_example_docs(EXAMPLES_ROOT)
    committed_root = SNAPSHOT_ROOT / "examples"
    committed = {
        str(path.relative_to(committed_root)): path.read_text(encoding="utf-8")
        for path in sorted(committed_root.rglob("*.md"))
    }
    assert committed == expected, (
        "examples snapshot is stale — run: python scripts/sync_knowledge.py"
    )

"""SDK docs snapshot plus the live capability catalog as a bounded knowledge base.

The authoring agent reads this through two read-only tools (see ``tools.py``).
Markdown under ``sdk/`` is committed output of ``scripts/sync_knowledge.py`` and a
pytest staleness gate keeps it byte-identical to the parent repo's ``docs/`` tree.
The catalog document is rendered from the live registry at load time, so that part
can never drift. Every read path is capped: search hits, snippet length, and
section length all have hard bounds so tool output cannot blow the token budget.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from linch_studio.catalog.registry import CATALOG

SNIPPET_CHARS = 240
SECTION_CHARS = 6_000
SEARCH_LIMIT = 8
DOCUMENT_CHARS = 8_000

CATALOG_DOC_PATH = "catalog/capabilities.md"
STUDIO_DOC_PATH = "studio/blueprint.md"

_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
# ``\w`` is Unicode aware.  The old ASCII-only expression split Vietnamese
# text into high-frequency one-letter fragments (for example ``lập`` became
# ``l`` and ``p``), causing unrelated provider pages to outrank scheduling.
_SLUG_TOKENS = re.compile(r"[\w]+", re.UNICODE)
_QUERY_TOKENS = re.compile(r"[\w.]+", re.UNICODE)
_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "the",
        "for",
        "of",
        "to",
        "in",
        "on",
        "is",
        "are",
        "i",
        "we",
        "it",
        "có",
        "và",
        "là",
        "cho",
        "về",
        "một",
        "các",
        "tôi",
        "bạn",
        "này",
        "đó",
        "thì",
        "với",
        "của",
    }
)
# Small, deliberately auditable bilingual expansion table.  It does not
# translate prose or add claims; it only makes the English SDK corpus reachable
# from the Vietnamese queries Studio users naturally type.
_QUERY_EXPANSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lập lịch", ("schedule", "scheduler", "cron")),
    ("lịch", ("schedule", "scheduler", "cron")),
    ("vòng lặp", ("loop", "looprunner")),
    ("đa tác tử", ("multi agent", "subagent", "workflow")),
    ("nhiều agent", ("multi agent", "subagent", "workflow")),
    ("tác tử", ("agent", "subagent")),
    ("triển khai", ("implementation", "example", "quickstart")),
    ("mẫu code", ("example", "examples")),
)


@dataclass(frozen=True, slots=True)
class Section:
    slug: str
    heading: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class SearchHit:
    anchor: str
    heading: str
    snippet: str
    score: int


@dataclass(frozen=True, slots=True)
class SectionText:
    text: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class DocumentText:
    """A bounded paginated document fragment for iterative retrieval."""

    path: str
    text: str
    offset: int
    next_offset: int | None


class UnknownAnchorError(LookupError):
    def __init__(self, anchor: str, suggestions: tuple[str, ...]) -> None:
        super().__init__(f"unknown section anchor: {anchor}")
        self.anchor = anchor
        self.suggestions = suggestions


def slugify(heading: str) -> str:
    """Turn a heading into a stable, file-scoped anchor slug (underscores survive)."""
    return "-".join(_SLUG_TOKENS.findall(heading.replace("`", "").lower()))


def parse_sections(text: str) -> list[Section]:
    """Split markdown into h1-h3 sections, ignoring ``#`` lines inside ``` fences."""
    lines = text.splitlines()
    headings: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line)
        if match:
            headings.append((number, match.group(2)))

    sections: list[Section] = []
    used: dict[str, int] = {}
    for index, (start, heading) in enumerate(headings):
        end = headings[index + 1][0] - 1 if index + 1 < len(headings) else len(lines)
        slug = slugify(heading) or "section"
        used[slug] = used.get(slug, 0) + 1
        if used[slug] > 1:
            slug = f"{slug}-{used[slug]}"
        sections.append(Section(slug=slug, heading=heading, start_line=start, end_line=end))
    return sections


def build_toc(files: dict[str, str]) -> dict[str, object]:
    """Build the deterministic table of contents for a path→markdown mapping."""
    entries = []
    for path in sorted(files):
        sections = parse_sections(files[path])
        entries.append(
            {
                "path": path,
                "title": sections[0].heading if sections else Path(path).stem,
                "sections": [
                    {
                        "slug": item.slug,
                        "heading": item.heading,
                        "startLine": item.start_line,
                        "endLine": item.end_line,
                    }
                    for item in sections
                ],
            }
        )
    return {"files": entries}


def collect_sdk_docs(docs_root: Path) -> dict[str, str]:
    """Collect the SDK doc files the snapshot mirrors, keyed by snapshot-relative path."""
    collected: dict[str, str] = {}
    for subdir in ("usage", "architecture"):
        for path in sorted((docs_root / subdir).glob("*.md")):
            collected[f"{subdir}/{path.name}"] = path.read_text(encoding="utf-8")
    versioning = docs_root / "versioning.md"
    if versioning.is_file():
        collected["versioning.md"] = versioning.read_text(encoding="utf-8")
    return collected


def collect_example_docs(examples_root: Path) -> dict[str, str]:
    """Collect repository examples into a Markdown-shaped, read-only corpus.

    Examples are source material, not files the support agent may execute or
    modify.  Python sources are fenced so headings inside code cannot affect the
    section index.  The sync script commits this corpus next to the docs snapshot
    for installed Studio builds where the parent repository is unavailable.
    """

    collected: dict[str, str] = {}
    if not examples_root.is_dir():
        return collected
    for path in sorted(examples_root.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".md"}:
            continue
        relative = path.relative_to(examples_root).as_posix()
        source = path.read_text(encoding="utf-8")
        if path.suffix == ".md":
            text = source
        else:
            text = f"# Example: examples/{relative}\n\n```python\n{source}\n```\n"
        collected[relative + ".md"] = text
    return collected


def _query_terms(query: str) -> tuple[str, ...]:
    """Return meaningful normalized terms plus narrowly scoped Vietnamese aliases."""

    lowered = unicodedata.normalize("NFC", query).casefold()
    expanded = [lowered]
    for phrase, aliases in _QUERY_EXPANSIONS:
        if phrase in lowered:
            expanded.extend(aliases)
    tokens: list[str] = []
    for text in expanded:
        for token in _QUERY_TOKENS.findall(text):
            # One-character terms cannot meaningfully rank this corpus and were
            # the source of extreme false positives for accented-language input.
            if len(token) >= 2 and token not in _QUERY_STOPWORDS:
                tokens.append(token)
    return tuple(dict.fromkeys(tokens))


def _expanded_query_terms(query: str) -> frozenset[str]:
    """Terms introduced by a bilingual alias rather than typed literally."""

    lowered = unicodedata.normalize("NFC", query).casefold()
    terms: set[str] = set()
    for phrase, aliases in _QUERY_EXPANSIONS:
        if phrase not in lowered:
            continue
        for alias in aliases:
            terms.update(
                token
                for token in _QUERY_TOKENS.findall(alias.casefold())
                if len(token) >= 2 and token not in _QUERY_STOPWORDS
            )
    return frozenset(terms)


def _render_catalog_doc() -> str:
    version = CATALOG.version
    lines = [
        "# Capability catalog (generated from the live registry)",
        "",
        f"Revision {version.revision}; target linch `{version.target_linch}`. "
        "This document defines what Studio may generate; the docs explain how and why.",
        "",
        "Capability IDs below are catalog labels for tiers and badges — never YAML "
        "values. In blueprint YAML a tool's `kind` is exactly one of `function`, "
        "`class`, or `database`; `custom_todo` is a verifier `kind`, not a tool kind. "
        "Copy exact shapes from studio/blueprint.md.",
        "",
    ]
    for status in CATALOG.statuses:
        lines.append(f"- [{status.badge}] {status.description}")
    for area in sorted(CATALOG.areas, key=lambda item: item.order):
        lines.extend(["", f"## {area.id}", "", f"{area.title} — {area.description}", ""])
        for record in CATALOG.capabilities:
            if record.area.id == area.id:
                lines.append(f"- `{record.id}` [{record.status.badge}] {record.summary}")
    lines.extend(["", "## relation-matrix", ""])
    for relation in CATALOG.relation_matrix.relations:
        lines.append(
            f"- {relation.id}: {relation.source_kind} -> {relation.target_kind} "
            f"({relation.decision}) — {relation.reason}"
        )
    return "\n".join(lines) + "\n"


def _fan_out_fan_in_example() -> str:
    """Render a worked three-way fan-out/fan-in workflow with subagents.

    Built on the directed_workflow template's own starting point, then extended
    to the shape a search-only agent could not otherwise afford to derive from
    prose docs alone. Live regression: asked for three parallel specialist
    subagents merging into one report with no such worked example to copy, a
    build turn burned 450k-650k tokens re-deriving `dependsOn` fan-out/fan-in
    semantics from scratch and repeatedly crashed. Still validated through the
    strict Blueprint model, so it can never drift out of sync with the schema.
    """
    from linch_studio.spec import Blueprint, dump_blueprint
    from linch_studio.templates import build_template_data

    data = build_template_data("directed_workflow", "fan_out_fan_in_example")
    data["spec"]["subagents"] = [
        {
            "id": subagent_id,
            "displayName": display_name,
            "instructions": f"Review the diff for {focus} issues and report concise findings.",
            "tools": [],
        }
        for subagent_id, display_name, focus in (
            ("security_reviewer", "Security Reviewer", "security"),
            ("performance_reviewer", "Performance Reviewer", "performance"),
            ("style_reviewer", "Style Reviewer", "style"),
        )
    ]
    data["spec"]["triggers"] = [
        {
            "kind": "ci",
            "id": "on_pull_request",
            "displayName": "On Pull Request",
            "provider": "github_actions",
        }
    ]
    data["spec"]["routines"] = [
        {
            "kind": "workflow_run",
            "id": "run_review_workflow",
            "displayName": "Run Review Workflow",
            "triggers": ["on_pull_request"],
            "target": "review_workflow",
            "doneWhen": {
                "kind": "custom_todo",
                "id": "human_review_gate",
                "description": (
                    "TODO: a human must approve the merged review report before the routine "
                    "is marked done."
                ),
            },
        }
    ]
    data["spec"]["workflows"] = [
        {
            "kind": "directed",
            "id": "review_workflow",
            "displayName": "Review Workflow",
            "nodes": [
                *(
                    {
                        "type": "agent_call",
                        "id": node_id,
                        "label": label,
                        "prompt": (
                            "Review the raw unified diff supplied once by the host in the "
                            f"trigger envelope payload for {focus} concerns."
                        ),
                        "dependsOn": [],
                        "subagent": subagent_id,
                        "tools": [],
                    }
                    for node_id, label, focus, subagent_id in (
                        ("security_review", "Security Review", "security", "security_reviewer"),
                        (
                            "performance_review",
                            "Performance Review",
                            "performance",
                            "performance_reviewer",
                        ),
                        ("style_review", "Style Review", "style", "style_reviewer"),
                    )
                ),
                {
                    "type": "agent_call",
                    "id": "merge_reviews",
                    "label": "Merge Reviews",
                    "prompt": "Combine the three predecessor reviews into one report.",
                    "dependsOn": ["security_review", "performance_review", "style_review"],
                    "tools": [],
                },
            ],
            "output": "merge_reviews",
            "maxConcurrency": 3,
        }
    ]
    return dump_blueprint(Blueprint.model_validate(data, strict=True)).strip()


def _render_blueprint_reference() -> str:
    """Render the v1alpha2 blueprint shape as complete template-registry examples.

    Rendered at load time from the same registry that seeds new projects, so the
    examples can never drift from the strict spec models — a template that stops
    validating fails every test that touches this document.
    """
    from linch_studio.spec import dump_blueprint
    from linch_studio.templates import build_template

    def example(template: str) -> str:
        return dump_blueprint(build_template(template, "example")).strip()

    lines = [
        "# Studio blueprint reference (v1alpha2)",
        "",
        "The exact LinchProject YAML shape a blueprint must express, as complete",
        "valid examples rendered from Studio's own template registry. Field names",
        "are camelCase on the wire. Do not invent fields these examples and the",
        "capability catalog do not show.",
        "",
        "## Directed workflow example",
        "",
        "```yaml",
        example("directed_workflow"),
        "```",
        "",
        "## Fan-out/fan-in workflow example",
        "",
        "One producer, three parallel specialist subagents, one merge step. Copy",
        "this shape directly for any 'parallel reviewers' or 'multiple specialists'",
        "request instead of inventing dependsOn edges from scratch.",
        "",
        "```yaml",
        _fan_out_fan_in_example(),
        "```",
        "",
        "## Routine and cron trigger example",
        "",
        "```yaml",
        example("routine"),
        "```",
        "",
        "## Goal-verified completion example",
        "",
        "```yaml",
        example("goal_verified"),
        "```",
    ]
    return "\n".join(lines) + "\n"


class KnowledgeBase:
    """In-memory, read-only index over the docs snapshot and the catalog document.

    Args:
        root: Snapshot directory containing ``sdk/`` and ``toc.json``; defaults to
            the packaged snapshot next to this module.
    """

    def __init__(self, root: Path | None = None) -> None:
        snapshot_root = root if root is not None else Path(__file__).resolve().parent
        self._files: dict[str, str] = {}
        sdk_root = snapshot_root / "sdk"
        if sdk_root.is_dir():
            for path in sorted(sdk_root.rglob("*.md")):
                self._files[str(path.relative_to(sdk_root))] = path.read_text(encoding="utf-8")
        examples_root = snapshot_root / "examples"
        if examples_root.is_dir():
            for path in sorted(examples_root.rglob("*.md")):
                self._files[f"examples/{path.relative_to(examples_root).as_posix()}"] = (
                    path.read_text(encoding="utf-8")
                )
        self._files[CATALOG_DOC_PATH] = _render_catalog_doc()
        self._files[STUDIO_DOC_PATH] = _render_blueprint_reference()
        self._sections: dict[str, dict[str, Section]] = {
            path: {section.slug: section for section in parse_sections(text)}
            for path, text in self._files.items()
        }
        self._anchors: tuple[str, ...] = tuple(
            f"{path}#{slug}" for path in sorted(self._sections) for slug in self._sections[path]
        )

    def search(self, query: str, *, limit: int = SEARCH_LIMIT) -> list[SearchHit]:
        """Rank sections with phrase, heading, path, and meaningful-term weighting.

        The corpus is intentionally compact, so a deterministic scorer is more
        inspectable than a hidden embedding dependency.  It still has the useful
        RAG properties Studio needs: exact symbols and paths dominate, headings
        beat body prose, and phrase matches beat disconnected token matches.
        """

        tokens = _query_terms(query)
        if not tokens:
            return []
        normalized_query = unicodedata.normalize("NFC", query).casefold().strip()
        expanded_terms = _expanded_query_terms(query)
        hits: list[SearchHit] = []
        for path, sections in self._sections.items():
            lines = self._files[path].splitlines()
            for section in sections.values():
                body = lines[section.start_line - 1 : section.end_line]
                text = "\n".join(body).casefold()
                heading = section.heading.casefold()
                path_lower = path.casefold()
                score = 0
                matched_terms = 0
                for token in tokens:
                    # Whole-token-ish occurrences avoid scoring `agent` in
                    # unrelated identifiers as highly as an explicit symbol.
                    occurrences = len(re.findall(rf"(?<!\w){re.escape(token)}(?!\w)", text))
                    if occurrences:
                        matched_terms += 1
                    # Code examples can repeat `agent` dozens of times. Cap
                    # term frequency so covering several query concepts beats
                    # a long unrelated file that repeats one generic word.
                    weight = 3 if token in expanded_terms else 1
                    score += min(occurrences, 4) * weight
                    if token in heading:
                        score += 12 * weight
                    if token in path_lower:
                        score += 8 * weight
                score += matched_terms * 3
                if len(normalized_query) >= 4 and normalized_query in text:
                    score += 20
                if score <= 0:
                    continue
                snippet = section.heading
                for line in body[1:]:
                    stripped = line.strip()
                    if stripped and any(token in stripped.casefold() for token in tokens):
                        snippet = stripped
                        break
                hits.append(
                    SearchHit(
                        anchor=f"{path}#{section.slug}",
                        heading=section.heading,
                        snippet=snippet[:SNIPPET_CHARS],
                        score=score,
                    )
                )
        hits.sort(key=lambda hit: (-hit.score, hit.anchor))
        return hits[: max(1, min(limit, SEARCH_LIMIT))]

    def section(self, anchor: str) -> SectionText:
        """Return one section's text, hard-capped at SECTION_CHARS.

        Raises:
            UnknownAnchorError: when the anchor does not exist; carries close matches.
        """
        path, _, slug = anchor.partition("#")
        section = self._sections.get(path, {}).get(slug)
        if section is None:
            close = difflib.get_close_matches(anchor, self._anchors, n=5, cutoff=0.5)
            if not close:
                close = [item for item in self._anchors if item.startswith(f"{path}#")][:5]
            raise UnknownAnchorError(anchor, tuple(close))
        lines = self._files[path].splitlines()
        text = "\n".join(lines[section.start_line - 1 : section.end_line])
        if len(text) > SECTION_CHARS:
            return SectionText(text=text[:SECTION_CHARS], truncated=True)
        return SectionText(text=text, truncated=False)

    def has_anchor(self, anchor: str) -> bool:
        """Return whether an evidence anchor belongs to this immutable corpus."""

        return anchor in self._anchors

    def list_documents(self, *, prefix: str | None = None) -> tuple[str, ...]:
        """List stable document paths without exposing any filesystem paths."""

        normalized = (prefix or "").strip().casefold()
        return tuple(
            path
            for path in sorted(self._files)
            if not normalized or path.casefold().startswith(normalized)
        )

    def find_symbol(self, symbol: str, *, limit: int = SEARCH_LIMIT) -> list[SearchHit]:
        """Find an exact symbol/string first, then use the normal ranker as a fallback."""

        cleaned = symbol.strip()
        if not cleaned:
            return []
        exact: list[SearchHit] = []
        needle = cleaned.casefold()
        for path, sections in self._sections.items():
            lines = self._files[path].splitlines()
            for section in sections.values():
                body = lines[section.start_line - 1 : section.end_line]
                text = "\n".join(body)
                if needle not in text.casefold():
                    continue
                snippet = next(
                    (line.strip() for line in body if needle in line.casefold() and line.strip()),
                    section.heading,
                )
                exact.append(
                    SearchHit(
                        anchor=f"{path}#{section.slug}",
                        heading=section.heading,
                        snippet=snippet[:SNIPPET_CHARS],
                        score=10_000 + text.casefold().count(needle),
                    )
                )
        exact.sort(key=lambda hit: (-hit.score, hit.anchor))
        return exact[: max(1, min(limit, SEARCH_LIMIT))] or self.search(cleaned, limit=limit)

    def document(self, path: str, *, offset: int = 0, limit: int = DOCUMENT_CHARS) -> DocumentText:
        """Read a bounded document page so agents can continue without a giant prompt."""

        if path not in self._files:
            close = difflib.get_close_matches(path, sorted(self._files), n=5, cutoff=0.5)
            raise UnknownAnchorError(path, tuple(close))
        if offset < 0:
            raise ValueError("offset must not be negative")
        if not 1 <= limit <= DOCUMENT_CHARS:
            raise ValueError(f"limit must be between 1 and {DOCUMENT_CHARS}")
        source = self._files[path]
        text = source[offset : offset + limit]
        end = offset + len(text)
        return DocumentText(
            path=path,
            text=text,
            offset=offset,
            next_offset=end if end < len(source) else None,
        )

    def toc_prompt(self, *, include_examples: bool = False) -> str:
        """Render the compact file-level table of contents for the system prompt."""
        lines = []
        for path in sorted(self._sections):
            if not include_examples and path.startswith("examples/"):
                continue
            sections = self._sections[path]
            title = next(iter(sections.values())).heading if sections else Path(path).stem
            lines.append(f"{path} — {title}")
        return "\n".join(lines)


__all__ = [
    "CATALOG_DOC_PATH",
    "DOCUMENT_CHARS",
    "SEARCH_LIMIT",
    "STUDIO_DOC_PATH",
    "SECTION_CHARS",
    "SNIPPET_CHARS",
    "KnowledgeBase",
    "DocumentText",
    "SearchHit",
    "Section",
    "SectionText",
    "UnknownAnchorError",
    "build_toc",
    "collect_sdk_docs",
    "collect_example_docs",
    "parse_sections",
    "slugify",
]

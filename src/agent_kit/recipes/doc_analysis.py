"""Document / image analysis recipe.

Creates an agent that extracts structured information from images or
documents and returns a typed ``{entities, summary}`` result.

Exercises: :class:`~agent_kit.types.OutputSchema`, image inputs via
``RunOptions.images``.

Quick start::

    import base64
    from agent_kit.recipes.doc_analysis import doc_agent
    from agent_kit.session import RunOptions
    from agent_kit.types import OutputSchema

    with open("invoice.png", "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    agent = doc_agent(model="gpt-5")
    session = await agent.session()

    async for event in session.run(
        "Extract all line items and totals.",
        RunOptions(images=[{"media_type": "image/png", "data": img_b64}]),
    ):
        if event.type == "result":
            print(event.structured_output)
            # {'entities': [...], 'summary': '...'}
"""

from __future__ import annotations

from typing import Any

from ..agent import Agent
from ..tools.registry import empty_tools
from ..types import OutputSchema
from . import build_agent

_EXTRACTION_SCHEMA = OutputSchema(
    name="doc_extraction",
    schema={
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "value": {"type": "string"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                    "required": ["type", "value"],
                },
                "description": "Named entities, values, and key facts extracted from the document.",
            },
            "summary": {
                "type": "string",
                "description": "A brief prose summary of the document's content.",
            },
        },
        "required": ["entities", "summary"],
        "additionalProperties": False,
    },
    strict=True,
)


def doc_agent(
    *,
    model: str,
    output_schema: OutputSchema | None = None,
    extra_instructions: str | None = None,
    **agent_kwargs: Any,
) -> Agent:
    """Create a document / image analysis agent.

    Images are passed per-run via ``RunOptions(images=[...])``.  Each image
    may be a URL (``{"url": "https://..."}``) or base64-encoded data
    (``{"media_type": "image/png", "data": "<b64>"}``).

    Args:
        model: LLM model identifier.
        output_schema: Override the default ``{entities, summary}`` schema.
        extra_instructions: Additional system instructions.
        **agent_kwargs: Forwarded to :func:`~agent_kit.recipes.build_agent`.

    Returns:
        An :class:`~agent_kit.agent.Agent` configured for structured document
        extraction.
    """
    base_instructions = (
        "You are an expert document analyst.  Carefully examine the provided "
        "document image(s) and extract structured information as requested.  "
        "Be precise: quote exact values, dates, and figures you can read.  "
        "If part of the document is illegible, note it in the summary rather "
        "than guessing."
    )
    if extra_instructions:
        base_instructions = f"{base_instructions}\n\n{extra_instructions}"

    eff_schema = output_schema or _EXTRACTION_SCHEMA

    return build_agent(
        model=model,
        system_instructions=base_instructions,
        tools=empty_tools(),
        output_schema=eff_schema,
        replace_default_system=True,
        **agent_kwargs,
    )

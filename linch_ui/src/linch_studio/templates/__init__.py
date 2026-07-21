"""Canonical Linch Studio blueprint templates shared by every authoring surface."""

from .registry import (
    DEFAULT_TEMPLATE,
    TEMPLATE_IDS,
    BlueprintTemplate,
    build_template,
    build_template_data,
    get_template,
    template_summaries,
)

__all__ = [
    "DEFAULT_TEMPLATE",
    "TEMPLATE_IDS",
    "BlueprintTemplate",
    "build_template",
    "build_template_data",
    "get_template",
    "template_summaries",
]

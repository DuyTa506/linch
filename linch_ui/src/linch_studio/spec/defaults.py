"""Factories for new Studio drafts backed by the shared template registry."""

from __future__ import annotations

from .models import Blueprint


def default_blueprint(
    name: str,
    *,
    title: str | None = None,
    template: str = "agent",
) -> Blueprint:
    """Create a fresh v1alpha2 blueprint from a product template.

    The import stays local so ``linch_studio.spec`` can expose this compatibility
    factory while the template registry itself validates against the strict
    :class:`Blueprint` model.
    """

    from linch_studio.templates import build_template

    try:
        return build_template(template, name, title=title)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc


create_default_blueprint = default_blueprint


__all__ = ["create_default_blueprint", "default_blueprint"]

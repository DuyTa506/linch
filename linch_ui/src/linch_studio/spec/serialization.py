"""Deterministic, alias-free YAML serialization for valid blueprint models."""

from __future__ import annotations

from typing import Any

import yaml  # type: ignore[reportMissingModuleSource]

from .canonical import canonical_data
from .models import Blueprint


class _BlueprintDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def dump_blueprint(blueprint: Blueprint) -> str:
    """Render canonical model fields as readable YAML with stable ordering."""

    rendered = yaml.dump(
        canonical_data(blueprint),
        Dumper=_BlueprintDumper,
        allow_unicode=True,
        default_flow_style=False,
        explicit_end=False,
        sort_keys=False,
        width=100,
    )
    return rendered if rendered.endswith("\n") else rendered + "\n"


dump_blueprint_yaml = dump_blueprint


__all__ = ["dump_blueprint", "dump_blueprint_yaml"]

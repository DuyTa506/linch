"""Keep the generated lifecycle catalog synchronized with source."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_generated_lifecycle_catalog_is_current() -> None:
    """Fail when the checked-in lifecycle table no longer matches the catalog."""
    result = subprocess.run(
        [sys.executable, "scripts/generate_event_catalog.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_hook_event_values_are_catalog_points() -> None:
    """Keep the public hook discriminator names aligned with the catalog."""
    from linch.hooks import HookEvent
    from linch.lifecycle import LIFECYCLE_POINTS

    hook_points = {
        point.name
        for point in LIFECYCLE_POINTS
        if isinstance(point._payload_ref, str)
        and point._payload_ref.startswith("linch.hooks.contexts.")
    }
    assert {event.value for event in HookEvent} == hook_points
    assert all(point.mode == "serial" for point in LIFECYCLE_POINTS if point.name in hook_points)

"""Linch kernel — a dependency-free "Cordis-lite" plugin substrate.

The kernel provides the three primitives every ported dsh idea builds on:

- :class:`Disposable` / :class:`EffectScope` — reversible effects (teardown that
  unwinds in reverse registration order).
- :class:`EventBus` — a typed event bus with emit / serial / waterfall dispatch.
- :class:`Context` — a per-agent service container and extension surface.

This package imports nothing outside ``linch.errors`` and the stdlib, keeping the
kernel free of provider SDKs or vendor dependencies.
"""

from __future__ import annotations

from .context import Context
from .effects import Disposable, EffectScope
from .events import EventBus, Next

__all__ = ["Context", "Disposable", "EffectScope", "EventBus", "Next"]

"""Reusable ``tools/execute`` around-wrappers for :class:`~linch.tools.pipeline.ToolPipeline`.

These are optional building blocks an embedder registers on an agent's
``tool_pipeline``. ``timeout`` and ``metrics`` ship here; retry stays a built-in
agent option (``RetryOptions``) rather than a wrapper, to avoid two competing
retry mechanisms.
"""

from __future__ import annotations

from .metrics import metrics_wrapper
from .timeout import timeout_wrapper

__all__ = ["metrics_wrapper", "timeout_wrapper"]

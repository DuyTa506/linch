from __future__ import annotations

import pytest

from linch.mcp.config import McpHttpServerConfig, McpStdioServerConfig


@pytest.mark.parametrize(
    ("timeout", "config_type"),
    [
        (True, McpStdioServerConfig),
        (True, McpHttpServerConfig),
        (None, McpHttpServerConfig),
        (0, McpHttpServerConfig),
        (float("nan"), McpHttpServerConfig),
    ],
)
def test_mcp_config_rejects_invalid_timeout(timeout, config_type):
    with pytest.raises((TypeError, ValueError), match="timeout_s"):
        config_type(timeout_s=timeout)


@pytest.mark.parametrize(
    ("timeout", "config_type"),
    [
        # int: skips the isfinite check entirely (short-circuited by the
        # isinstance(..., float) guard); float: actually calls math.isfinite
        # and gets True. One of each, on each independent __post_init__ call site.
        (1, McpStdioServerConfig),
        (0.5, McpHttpServerConfig),
    ],
)
def test_mcp_config_accepts_finite_positive_timeout(timeout, config_type):
    assert config_type(timeout_s=timeout).timeout_s == timeout

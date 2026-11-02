from __future__ import annotations

import pytest

from linch.mcp.config import McpHttpServerConfig, McpStdioServerConfig


@pytest.mark.parametrize("config_type", [McpStdioServerConfig, McpHttpServerConfig])
@pytest.mark.parametrize(
    "timeout", [True, False, float("nan"), float("inf"), -float("inf"), "1", None, 0, -1]
)
def test_mcp_config_rejects_invalid_timeout(config_type, timeout):
    with pytest.raises((TypeError, ValueError), match="timeout_s"):
        config_type(timeout_s=timeout)


@pytest.mark.parametrize("config_type", [McpStdioServerConfig, McpHttpServerConfig])
@pytest.mark.parametrize("timeout", [1, 0.5])
def test_mcp_config_accepts_finite_positive_timeout(config_type, timeout):
    assert config_type(timeout_s=timeout).timeout_s == timeout

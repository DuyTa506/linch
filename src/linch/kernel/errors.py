"""Errors shared by the dependency-free kernel."""


class LinchError(Exception):
    """Base class for errors raised by Linch."""

    kind = "linch"
    retryable = False


class ConfigError(LinchError):
    """Invalid kernel or agent configuration."""

    kind = "config"

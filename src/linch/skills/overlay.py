from __future__ import annotations


def resolve_model_override(override: str, session_model: str) -> str:
    """Preserve the session's one-million-token suffix for a skill override.

    Args:
        override: Model name requested by the skill.
        session_model: Model name configured for the current session.

    Returns:
        The override, with ``[1m]`` appended when the session requires it.
    """
    if override.endswith("[1m]") or not session_model.endswith("[1m]"):
        return override
    return f"{override}[1m]"

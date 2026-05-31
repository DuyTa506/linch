from __future__ import annotations


def resolve_model_override(override: str, session_model: str) -> str:
    if override.endswith("[1m]") or not session_model.endswith("[1m]"):
        return override
    return f"{override}[1m]"

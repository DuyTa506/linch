from __future__ import annotations


def wrap_in_system_reminder(text: str) -> str:
    """Wrap skill context in the system-reminder envelope.

    Args:
        text: Skill context to inject into the model-visible prompt.

    Returns:
        The text enclosed by ``<system-reminder>`` tags.
    """
    return f"<system-reminder>\n{text}\n</system-reminder>"

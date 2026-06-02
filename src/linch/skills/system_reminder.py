from __future__ import annotations


def wrap_in_system_reminder(text: str) -> str:
    return f"<system-reminder>\n{text}\n</system-reminder>"

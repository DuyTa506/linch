from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any


def error_status(err: Exception) -> int | None:
    value = getattr(err, "status_code", None) or getattr(err, "status", None)
    return int(value) if isinstance(value, int) else None


def error_body(err: Exception) -> Any:
    body = getattr(err, "body", None)
    if body is not None:
        return body
    response = getattr(err, "response", None)
    if response is not None:
        body = getattr(response, "body", None)
        if body is not None:
            return body
        try:
            return response.json()
        except Exception:
            return None
    return None


def nested_error(err: Exception) -> dict[str, Any]:
    body = error_body(err)
    if isinstance(body, dict):
        raw = body.get("error", body)
        if isinstance(raw, dict):
            return raw
    raw_error = getattr(err, "error", None)
    if isinstance(raw_error, dict):
        return raw_error
    return {}


def error_code(err: Exception) -> str | None:
    raw = nested_error(err).get("code") or nested_error(err).get("type")
    return str(raw) if isinstance(raw, str) and raw else None


def error_message(err: Exception) -> str:
    raw = nested_error(err).get("message")
    if isinstance(raw, str) and raw:
        return raw
    return str(err)


def retry_after_seconds(err: Exception) -> float | None:
    response = getattr(err, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        headers = getattr(err, "headers", None)
    if not headers:
        return None
    raw = None
    for key in ("retry-after", "Retry-After", "retry_after"):
        try:
            raw = headers.get(key)
        except Exception:
            raw = None
        if raw is not None:
            break
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    text = str(raw).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


def is_prompt_length_error(err: Exception) -> bool:
    code = error_code(err)
    if code == "context_length_exceeded":
        return True
    message = error_message(err).lower()
    clear_phrases = (
        "prompt is too long",
        "maximum context length",
        "context length exceeded",
        "input is too long",
    )
    if any(phrase in message for phrase in clear_phrases):
        return True
    return "tokens" in message and "maximum" in message and "prompt" in message

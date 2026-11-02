from __future__ import annotations

import json
from urllib import error, request
from urllib.parse import urlsplit, urlunsplit

_DEFAULT_CONTEXT_WINDOW = 128_000
_SOLE_MODEL = "*"


def probe_context_windows(
    base_url: str | None,
    *,
    api_key: str | None,
    default_headers: dict[str, str] | None,
    timeout: float,
) -> dict[str, int]:
    """Read positive per-model ``max_model_len`` values from ``/v1/models``."""
    if not base_url:
        return {}
    headers = {"Accept": "application/json"}
    if default_headers:
        headers.update(default_headers)
    if api_key is not None and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        url = _models_url(base_url)
        req = request.Request(url, headers=headers, method="GET")
        with request.urlopen(req, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, ValueError, error.URLError, error.HTTPError, TypeError):
        return {}
    rows = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return {}
    result: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = row.get("id")
        value = row.get("max_model_len")
        if not isinstance(model, str) or not model:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        result[model] = value
        if len(rows) == 1:
            result[_SOLE_MODEL] = value
    return result


def probed_context_window(cache: dict[str, int] | None, model: str) -> int:
    """Return the probed capacity, trusting a single-model server for any alias."""
    cache = cache or {}
    return cache.get(model, cache.get(_SOLE_MODEL, _DEFAULT_CONTEXT_WINDOW))


def _models_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    split = urlsplit(base)
    path = split.path.rstrip("/")
    if path.endswith("/v1"):
        path = f"{path}/models"
    else:
        path = f"{path}/v1/models"
    return urlunsplit((split.scheme, split.netloc, path, "", ""))

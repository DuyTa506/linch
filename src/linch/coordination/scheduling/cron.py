"""A neutral, dependency-free 5-field cron utility (minute resolution).

Fields: ``minute hour day-of-month month day-of-week`` with ``*``, ranges
(``a-b``), lists (``a,b``), and steps (``*/n``, ``a-b/n``). Day-of-week is
``0-6`` with Sunday = 0 (``7`` also accepted for Sunday). Matching is evaluated
in UTC. This is a *mechanism*: what a schedule triggers is embedder policy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# (low, high) inclusive bounds per field.
_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
# A brute-force next-run search caps here so an unsatisfiable expression fails
# loudly instead of spinning forever. The window spans a full leap cycle (4
# years) so a legitimate rare match like "Feb 29" is never falsely rejected.
_MAX_SEARCH_MINUTES = 4 * 366 * 24 * 60


def _parse_field(field: str, low: int, high: int, *, name: str) -> set[int]:
    """Expand one cron field to the set of integers it matches."""
    if not field:
        raise ValueError(f"empty {name} field")
    values: set[int] = set()
    for part in field.split(","):
        rng, _, step_s = part.partition("/")
        if _ and step_s == "":
            raise ValueError(f"invalid step in {name} field: {part!r}")
        try:
            step = int(step_s) if step_s else 1
        except ValueError as exc:
            raise ValueError(f"invalid step in {name} field: {part!r}") from exc
        if step <= 0:
            raise ValueError(f"step must be positive in {name} field: {part!r}")
        if rng == "*":
            start, end = low, high
        elif "-" in rng:
            a, _, b = rng.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError as exc:
                raise ValueError(f"invalid range in {name} field: {part!r}") from exc
        else:
            try:
                start = end = int(rng)
            except ValueError as exc:
                raise ValueError(f"invalid value in {name} field: {part!r}") from exc
        # Day-of-week 7 is an alias for Sunday (0). Accept 7 as a single value
        # or a range endpoint (e.g. "7", "0-7", "6-7"), then fold it to 0 after
        # expansion so matching (0..6) still works without collapsing the range.
        effective_high = 7 if name == "day-of-week" else high
        if start < low or end > effective_high or start > end:
            raise ValueError(f"{name} value out of range [{low},{high}]: {part!r}")
        expanded = set(range(start, end + 1, step))
        if name == "day-of-week" and 7 in expanded:
            expanded.discard(7)
            expanded.add(0)
        values.update(expanded)
    return values


def validate_cron(expr: str) -> str:
    """Return *expr* unchanged if it is a well-formed 5-field cron, else raise."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(fields)}: {expr!r}")
    for field, (low, high), name in zip(fields, _FIELD_BOUNDS, _FIELD_NAMES, strict=True):
        _parse_field(field, low, high, name=name)
    return expr


def cron_matches(expr: str, when: datetime) -> bool:
    """True if *when* (any tz; compared in UTC) satisfies the cron *expr*."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields: {expr!r}")
    minute = _parse_field(fields[0], 0, 59, name="minute")
    hour = _parse_field(fields[1], 0, 23, name="hour")
    dom = _parse_field(fields[2], 1, 31, name="day-of-month")
    month = _parse_field(fields[3], 1, 12, name="month")
    dow = _parse_field(fields[4], 0, 6, name="day-of-week")
    when = when.astimezone(timezone.utc)
    # Python weekday(): Monday=0..Sunday=6; cron: Sunday=0..Saturday=6.
    cron_dow = (when.weekday() + 1) % 7
    return (
        when.minute in minute
        and when.hour in hour
        and when.day in dom
        and when.month in month
        and cron_dow in dow
    )


def next_cron_time(expr: str, after_epoch: float) -> float:
    """Epoch seconds of the next minute strictly after *after_epoch* matching *expr*."""
    validate_cron(expr)
    start = datetime.fromtimestamp(after_epoch, tz=timezone.utc).replace(second=0, microsecond=0)
    candidate = start + timedelta(minutes=1)
    for _ in range(_MAX_SEARCH_MINUTES):
        if cron_matches(expr, candidate):
            return candidate.timestamp()
        candidate += timedelta(minutes=1)
    raise ValueError(f"cron expression never matches within the search window: {expr!r}")

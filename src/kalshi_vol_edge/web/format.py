"""Number and time formatting shared by templates and charts. Missing values render as an em
dash rather than ``None`` or ``nan``."""

from __future__ import annotations

import math
from datetime import UTC, datetime

DASH = "\N{EM DASH}"
MINUS = "\N{MINUS SIGN}"


def _missing(value: float | None) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def usd(value: float | None, decimals: int = 2, signed: bool = False) -> str:
    """``$1,234.56``; with ``signed`` a leading ``+`` or a true minus sign."""
    if value is None or _missing(value):
        return DASH
    body = f"${abs(value):,.{decimals}f}"
    if signed:
        return ("+" if value > 0 else MINUS if value < 0 else "") + body
    return (MINUS if value < 0 else "") + body


def pct(value: float | None, decimals: int = 1, signed: bool = False) -> str:
    """A fraction as a percentage: ``0.385`` -> ``38.5%``."""
    if value is None or _missing(value):
        return DASH
    body = f"{abs(value) * 100:.{decimals}f}%"
    if signed:
        return ("+" if value > 0 else MINUS if value < 0 else "") + body
    return (MINUS if value < 0 else "") + body


def cents(value: float | None, signed: bool = False, decimals: int = 0) -> str:
    """A dollar price in cents: ``0.75`` -> ``75¢``."""
    if value is None or _missing(value):
        return DASH
    body = f"{abs(value) * 100:.{decimals}f}¢"
    if signed:
        return ("+" if value > 0 else MINUS if value < 0 else "") + body
    return (MINUS if value < 0 else "") + body


def num(value: float | None, decimals: int = 0) -> str:
    """A plain number with thousands separators."""
    if value is None or _missing(value):
        return DASH
    return (MINUS if value < 0 else "") + f"{abs(value):,.{decimals}f}"


def utc_time(ts: float | datetime | None, with_date: bool = False) -> str:
    """``03:45`` or ``Mar 23 03:45`` in UTC."""
    if ts is None:
        return DASH
    dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, UTC)
    return dt.astimezone(UTC).strftime("%b %d %H:%M" if with_date else "%H:%M")


def clock(minutes: float | None) -> str:
    """Minutes as ``m:ss``."""
    if minutes is None or _missing(minutes):
        return DASH
    total = max(0, round(minutes * 60))
    return f"{total // 60}:{total % 60:02d}"


def age(seconds: float | None) -> str:
    """A compact age: ``4s``, ``2m``, ``1h``."""
    if seconds is None:
        return "never"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def tone(value: float | None) -> str:
    """CSS tone class for a signed value."""
    if value is None or _missing(value) or value == 0:
        return ""
    return "pos" if value > 0 else "neg"


FILTERS = {
    "usd": usd,
    "pct": pct,
    "cents": cents,
    "num": num,
    "utc_time": utc_time,
    "clock": clock,
    "age": age,
    "tone": tone,
}

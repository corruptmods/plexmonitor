"""Duration, count, bitrate and timestamp formatting helpers.

format_duration() rolls through minutes, hours, days and months with no
cap, so a long uptime never collapses into a "99+ Hours" style ceiling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# (suffix, seconds). Months are 30 days and years 365 -- approximations, but
# this is a status readout, not an accounting ledger.
_UNITS = (
    ("y", 31_536_000),
    ("mo", 2_592_000),
    ("d", 86_400),
    ("h", 3_600),
    ("m", 60),
    ("s", 1),
)

# Durations shorter than this stay in days rather than rolling into months.
_MONTH_THRESHOLD = 90 * 86_400


def format_duration(seconds: float | None, max_units: int = 2, short: bool = True) -> str:
    """Human-readable duration with no upper cap.

        47 minutes      -> "47m"
        9h 5m           -> "9h 5m"
        3 days          -> "3d 0h"
        6 days 3 hours  -> "6d 3h"
        41 days         -> "41d 6h"
        7 months        -> "7mo 2d"

    max_units controls how many components are shown: 2 gives "6d 3h",
    3 would give "6d 3h 12m". Sub-minute durations always render as seconds
    so a freshly started server doesn't show a bare "0m".
    """
    if seconds is None:
        return "Unknown"
    try:
        total = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        return "Unknown"

    if total < 60:
        return f"{total}s"

    # Below the threshold, keep counting in days -- "41d 6h" reads better for a
    # server uptime than "1mo 11d". Months and years only kick in past 90 days,
    # where a raw day count stops being meaningful.
    skip_large = total < _MONTH_THRESHOLD

    parts: list[str] = []
    remaining = total
    started = False

    for suffix, size in _UNITS:
        if skip_large and suffix in ("y", "mo"):
            continue
        if suffix == "s" and started:
            # Don't tack seconds onto a duration measured in minutes or more.
            break
        value, remaining = divmod(remaining, size)
        if value == 0 and not started:
            continue
        started = True
        parts.append(f"{value}{suffix}" if short else f"{value} {suffix}")
        if len(parts) >= max_units:
            break

    return " ".join(parts) if parts else "0s"


def format_duration_long(seconds: float | None, max_units: int = 2) -> str:
    """Spelled-out duration: "7 Days 18 Hours" instead of "7d 18h"."""
    if seconds is None:
        return "Unknown"
    try:
        total = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        return "Unknown"
    if total < 60:
        return f"{total} Second{'s' if total != 1 else ''}"

    names = {"y": "Year", "mo": "Month", "d": "Day", "h": "Hour", "m": "Minute"}
    skip_large = total < _MONTH_THRESHOLD
    parts: list[str] = []
    remaining = total
    started = False
    for suffix, size in _UNITS:
        if suffix == "s" or (skip_large and suffix in ("y", "mo")):
            continue
        value, remaining = divmod(remaining, size)
        if value == 0 and not started:
            continue
        started = True
        unit = names[suffix]
        parts.append(f"{value} {unit}{'s' if value != 1 else ''}")
        if len(parts) >= max_units:
            break
    return " ".join(parts) if parts else "0 Minutes"


def format_hms(milliseconds: float | None, total_milliseconds: float | None = None) -> str:
    """Format a playback position as M:SS or H:MM:SS.

    The hour component is included when the *total* runtime is an hour or
    more, so a 2h film reads "0:07:15" rather than jumping between formats
    as it plays.
    """
    try:
        ms = int(max(0, float(milliseconds or 0)))
    except (TypeError, ValueError):
        ms = 0
    try:
        total_ms = int(float(total_milliseconds)) if total_milliseconds else ms
    except (TypeError, ValueError):
        total_ms = ms

    seconds = ms // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    if total_ms >= 3_600_000:
        # Under an hour into content longer than an hour: "19:17" reads fine
        # and saves two monospace cells on what is often the widest line.
        return f"{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_count(value: int | float | None) -> str:
    """Thousands-separated integer, or an em dash when unknown."""
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def format_bitrate(kbps: float | str | None) -> str:
    """kbps (as Tautulli reports it) -> "8.1 Mbps" / "560 kbps"."""
    try:
        value = float(kbps)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    # Compare the ROUNDED value: 999.6 kbps rounds to "1000 kbps" otherwise.
    if round(value) >= 1000:
        return f"{value / 1000:.1f} Mbps"
    return f"{value:.0f} kbps"


def discord_timestamp(dt: Optional[datetime], style: str = "R") -> str:
    """Render a Discord <t:...> tag, which each client localises itself.

    Style "R" gives "3 days ago"; "f" gives a full local date and time.
    Needs no timezone configuration at all.
    """
    if dt is None:
        return "Unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"

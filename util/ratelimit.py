"""Read Discord's rate-limit headers instead of guessing at them.

Every REST response carries the state of the bucket it consumed:

    X-RateLimit-Limit        requests allowed in this window
    X-RateLimit-Remaining    how many are left
    X-RateLimit-Reset-After  seconds until the window refills
    X-RateLimit-Bucket       which bucket this route belongs to
    X-RateLimit-Scope        on a 429: "user", "global" or "shared"

The bot previously inferred all of this from timing -- widening intervals
after a stall and narrowing them after a clean run -- which meant it could
only learn a limit by exceeding it. Reading the headers turns that around:
it can stop one request short of the limit and wait exactly as long as
Discord says, so the 429 never happens.

X-RateLimit-Scope is the other prize. It states whose limit was hit:

    user    this bot's own budget
    shared  a budget shared with everything else touching that resource
    global  the 50-requests-per-second ceiling

Wired in via aiohttp's TraceConfig, which discord.py accepts through its
public `http_trace` parameter -- no monkey-patching of internals.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("plexmonitor.ratelimit")

# One fully-detailed 429 per this many seconds; the rest are counted
# and reported as a single summary line when the window rolls over.
NOISE_WINDOW = 600.0
# A message edit's route, and the channel it's in (its bucket's key).
_EDIT_ROUTE = re.compile(r"/channels/(\d+)/messages/\d+")
# Channels whose edit buckets are remembered at once (the dashboard's,
# the cards', the request panel's...); the oldest reading goes first.
MAX_CHANNELS = 32

try:
    import aiohttp
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False


def _as_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class RateLimitObserver:
    """Records the rate-limit state Discord reports on every response."""

    def __init__(self) -> None:
        # Most recent state of a message edit's bucket, in any channel, and
        # per channel. Discord keys the edit bucket by channel, so the cards
        # channel running dry says nothing about the dashboard's.
        self.last_edit: Optional[Dict[str, Any]] = None
        self.edits: Dict[str, Dict[str, Any]] = {}
        self.last_429: Optional[Dict[str, Any]] = None
        self.count_429 = 0
        # Log aggregation. A common steady state is a shared-scope
        # 429 roughly every scheduled edit -- known, absorbed, harmless --
        # and printing two WARNING lines per occurrence buried everything
        # else in the log. First one in a window logs in full; repeats are
        # counted and summarised when the window rolls.
        self._noise_window_start = None  # None = nothing logged yet
        self._noise_suppressed = 0

    def record(self, method: str, url: str, headers, status: int) -> None:
        info = {
            "limit": _as_int(headers.get("X-RateLimit-Limit")),
            "remaining": _as_int(headers.get("X-RateLimit-Remaining")),
            "reset_after": _as_float(headers.get("X-RateLimit-Reset-After")),
            "retry_after": _as_float(headers.get("Retry-After")),
            "bucket": headers.get("X-RateLimit-Bucket"),
            "scope": headers.get("X-RateLimit-Scope"),
            "global": headers.get("X-RateLimit-Global"),
            "status": status,
            "at": time.monotonic(),
        }
        if status == 429:
            self.count_429 += 1
            self.last_429 = info
            now = time.monotonic()
            if (self._noise_window_start is not None
                    and now - self._noise_window_start < NOISE_WINDOW):
                # Same steady-state noise; count it and stay quiet. The
                # observer still records everything -- only the LOG is
                # summarised, so the morning grep counts stay meaningful
                # via the summary line's total.
                self._noise_suppressed += 1
                self._record_edit(method, url, info)
                return
            if self._noise_suppressed:
                logger.warning(
                    "... plus %d more 429s in the last %.0f min (all "
                    "absorbed by retry)",
                    self._noise_suppressed, NOISE_WINDOW / 60.0)
            self._noise_window_start = now
            self._noise_suppressed = 0
            # Scope is the diagnostic that timing alone could never provide.
            #
            # Retry-After is a HEADER and Discord rounds it; the JSON body
            # carries the real figure, which is what discord.py sleeps on.
            # The two have been seen to disagree by 85x, and
            # quoting the header as though it were the penalty made a
            # serious problem look trivial. Reset-After is the honest
            # window, so it is printed alongside.
            logger.warning(
                "429 on %s %s -- scope=%s bucket=%s "
                "retry_after_header=%.1fs reset_after=%.1fs "
                "(limit=%s remaining=%s global=%s)",
                method, _short(url), info["scope"] or "?",
                info["bucket"] or "?", info["retry_after"] or 0.0,
                info["reset_after"] if info["reset_after"] is not None else -1.0,
                info["limit"], info["remaining"], info["global"] or "-",
            )
            # Everything Discord sent, once per 429, so a diagnosis never
            # again rests on the two fields this line happened to pick.
            try:
                interesting = {k: v for k, v in headers.items()
                               if k.lower().startswith("x-ratelimit")
                               or k.lower() in ("retry-after", "via", "date")}
                logger.debug("429 headers: %s", interesting)
            except Exception:
                pass

        self._record_edit(method, url, info)

    def _record_edit(self, method: str, url: str,
                     info: Dict[str, Any]) -> None:
        # Channel message edits only. An interaction edit (PATCH
        # /webhooks/{app}/{token}/messages/@original) is keyed by the token,
        # a different bucket; recording it here let a page flip's fresh
        # bucket stand in for the channel's and misjudge the next refresh.
        channel = _edit_channel(method, url)
        if channel is None:
            return
        self.last_edit = info
        self.edits.pop(channel, None)
        self.edits[channel] = info
        while len(self.edits) > MAX_CHANNELS:
            self.edits.pop(next(iter(self.edits)))

    def _edit(self, channel_id: Any = None) -> Optional[Dict[str, Any]]:
        """The edit bucket's last reading: in that channel, or anywhere."""
        if channel_id:
            return self.edits.get(str(channel_id))
        return self.last_edit

    # ------------------------------------------------------------------
    # Consumers. Each takes the channel whose bucket it's about; without
    # one, the last edit anywhere.

    def edit_wait(self, channel_id: Any = None) -> float:
        """Seconds to wait before another message edit is safe.

        Returns 0 when the bucket has room, or when Discord has told us
        nothing yet.
        """
        info = self._edit(channel_id)
        if not info:
            return 0.0
        remaining = info.get("remaining")
        reset_after = info.get("reset_after")
        if remaining is None or reset_after is None:
            return 0.0
        if remaining > 0:
            return 0.0
        elapsed = time.monotonic() - info["at"]
        return max(0.0, reset_after - elapsed)

    def edit_headroom(self, channel_id: Any = None) -> Optional[int]:
        """Requests left in the edit bucket, or None if unknown.

        Returns None once the bucket's window has elapsed: the count was
        true at response time, but after reset the bucket has refilled and
        the stale zero would read as "still empty". That misread charged
        phantom soft limits against perfectly healthy edits.
        """
        info = self._edit(channel_id)
        if not info:
            return None
        reset_after = info.get("reset_after")
        if reset_after is not None:
            if time.monotonic() - info["at"] >= reset_after:
                return None
        return info.get("remaining")

    def edit_bucket_full(self, channel_id: Any = None) -> bool:
        """True only when Discord's last word was an UNTOUCHED edit bucket.

        Deliberately strict: unknown, stale or partial state all read as
        False. This is the evidence a caller needs before deciding a
        throttle has outlived its cause, so "no idea" must never look like
        "all clear".
        """
        info = self._edit(channel_id)
        if not info:
            return False
        limit = info.get("limit")
        remaining = info.get("remaining")
        if not limit or remaining is None:
            return False
        reset_after = info.get("reset_after")
        if reset_after is not None and time.monotonic() - info["at"] >= reset_after:
            # Window elapsed: the bucket has refilled to full by definition.
            return True
        return remaining >= limit - 1

    def describe(self, channel_id: Any = None) -> str:
        info = self._edit(channel_id)
        if not info:
            return "no rate-limit data yet"
        return (
            f"bucket={info.get('bucket') or '?'} "
            f"remaining={info.get('remaining')}/{info.get('limit')} "
            f"resets in {info.get('reset_after') or 0:.1f}s"
        )


# Interaction and webhook routes carry a token that lets anyone holding it
# post or edit as the bot for a while.
_TOKEN_SEGMENT = re.compile(r"(/(?:webhooks|interactions)/\d+/)[^/?#]+")


def _short(url: str) -> str:
    """Trim a Discord API url down to its route for logging, with any
    interaction or webhook token masked."""
    marker = "/api/v"
    index = url.find(marker)
    route = url[index:] if index >= 0 else url
    return _TOKEN_SEGMENT.sub(r"\1***", route)


def make_trace(observer: RateLimitObserver):
    """Build the aiohttp TraceConfig that feeds `observer`.

    Returns None when aiohttp is unavailable, so callers can degrade to the
    timing-based behaviour rather than failing to start.
    """
    if not AVAILABLE:
        return None

    trace = aiohttp.TraceConfig()

    async def on_request_end(session, context, params) -> None:
        try:
            observer.record(
                params.method,
                str(params.url),
                params.response.headers,
                params.response.status,
            )
        except Exception:  # never let instrumentation break a request
            logger.debug("Rate-limit observation failed", exc_info=True)

    trace.on_request_end.append(on_request_end)
    return trace


def _edit_channel(method: str, url: str) -> Optional[str]:
    """The channel of a channel message edit -- the bucket the dashboard
    paces itself by -- or None for anything else. Interaction edits (PATCH
    /webhooks/.../messages/@original) are keyed by the interaction token: a
    different bucket entirely."""
    if method != "PATCH":
        return None
    match = _EDIT_ROUTE.search(url)
    return match.group(1) if match else None

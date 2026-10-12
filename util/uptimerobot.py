"""UptimeRobot as the uptime source.

Drop-in alternative to util.kuma. UptimeRobot watches from outside the
network, so its numbers survive the things that make a self-hosted probe
lie: the container restarting, the host rebooting, the monitoring stack
going down with everything else it was meant to be watching. A local
monitor that is offline during an outage records no outage.

Uses the v2 API, which is the one that exposes uptime ratios for
arbitrary windows in a single call:

    POST https://api.uptimerobot.com/v2/getMonitors
        api_key=...&format=json&custom_uptime_ratios=1-7-30&logs=1

The response carries "custom_uptime_ratio" as a dash-joined string in the
same order the periods were requested -- "99.85-99.90-99.72" -- plus a
log of state transitions the uptime counter can be seeded from.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import aiohttp

logger = logging.getLogger("plexmonitor.uptimerobot")

API_URL = "https://api.uptimerobot.com/v2/getMonitors"

# Monitor status codes, from the API docs.
STATUS_PAUSED = 0
STATUS_NOT_CHECKED = 1
STATUS_UP = 2
STATUS_SEEMS_DOWN = 8
STATUS_DOWN = 9

# Log entry types.
LOG_DOWN = 1
LOG_UP = 2


class UptimeRobotClient:
    """Reads one monitor's status, uptime ratios and transition log."""

    def __init__(self, api_key: str, monitor_id: str | int = "",
                 timeout: float = 15.0) -> None:
        self.api_key = api_key
        self.monitor_id = str(monitor_id) if monitor_id else ""
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        # Populated by fetch(); reused by transitions() so seeding the
        # uptime counter costs no extra request.
        self._last_logs: list = []
        self._created_at: Optional[datetime] = None
        self._status: Optional[bool] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self.timeout)
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _call(self) -> Dict[str, Any]:
        payload = {
            "api_key": self.api_key,
            "format": "json",
            # Order matters: the response joins the ratios in this order.
            "custom_uptime_ratios": "1-7-30",
            "logs": "1",
            "logs_limit": "25",
            "response_times": "1",
            "response_times_limit": "1",
        }
        if self.monitor_id:
            payload["monitors"] = self.monitor_id
        session = await self._get_session()
        async with session.post(API_URL, data=payload) as response:
            response.raise_for_status()
            # The API answers 200 with stat=fail on bad input, so the HTTP
            # status alone is not enough to know the call worked.
            return await response.json(content_type=None)

    @staticmethod
    def _ratios(monitor: Dict[str, Any]) -> Tuple[Optional[float], ...]:
        raw = str(monitor.get("custom_uptime_ratio") or "")
        out: list = []
        for part in raw.split("-"):
            try:
                out.append(round(float(part), 2))
            except (TypeError, ValueError):
                out.append(None)
        while len(out) < 3:
            out.append(None)
        return tuple(out[:3])

    async def fetch(self) -> Dict[str, Any]:
        """Status plus uptime windows, in util.kuma's shape.

        Never raises: an uptime provider being unreachable should degrade
        the dashboard, not break the refresh loop.
        """
        result: Dict[str, Any] = {
            "status": None, "response_ms": None,
            "uptime_24h": None, "uptime_7d": None, "uptime_30d": None,
            "uptime_1y": None, "error": None,
        }
        try:
            data = await self._call()
        except Exception as exc:
            result["error"] = str(exc)
            logger.warning("UptimeRobot request failed: %s", exc)
            return result

        if str(data.get("stat")) != "ok":
            error = data.get("error") or {}
            message = error.get("message") or str(error) or "unknown error"
            result["error"] = message
            logger.warning("UptimeRobot returned an error: %s", message)
            return result

        monitors = data.get("monitors") or []
        if not monitors:
            result["error"] = "no monitors returned"
            logger.warning(
                "UptimeRobot returned no monitors%s",
                f" for id {self.monitor_id}" if self.monitor_id else "")
            return result

        monitor = monitors[0]
        code = monitor.get("status")
        if code == STATUS_UP:
            result["status"] = True
        elif code in (STATUS_SEEMS_DOWN, STATUS_DOWN):
            result["status"] = False
        # Paused or never-checked stays None: unknown is not the same as
        # down, and reporting it as down would start a false outage.

        day, week, month = self._ratios(monitor)
        result["uptime_24h"], result["uptime_7d"], result["uptime_30d"] = (
            day, week, month)
        try:
            all_time = monitor.get("all_time_uptime_ratio")
            if all_time is not None:
                result["uptime_1y"] = round(float(all_time), 2)
        except (TypeError, ValueError):
            pass

        times = monitor.get("response_times") or []
        if times:
            try:
                ms = int(times[0].get("value"))
                # UptimeRobot logs a failed or timed-out check as value -1
                # (and some monitor types as 0). Neither is a latency, and
                # rendering one as "-1 ms" makes the dashboard look broken
                # right after the outage it correctly detected. Unknown
                # stays None and the field simply is not shown.
                if ms > 0:
                    result["response_ms"] = ms
            except (TypeError, ValueError, AttributeError):
                pass

        self._last_logs = monitor.get("logs") or []
        self._status = result["status"]
        # When a monitor has simply never gone down, its log holds no
        # recovery to measure from -- the honest answer is "up since it was
        # created", which is still a real external fact rather than a
        # restart-scoped guess.
        try:
            created = monitor.get("create_datetime")
            self._created_at = (
                datetime.fromtimestamp(int(created), tz=timezone.utc)
                if created else None)
        except (TypeError, ValueError):
            self._created_at = None
        return result

    def transitions(self) -> Optional[Tuple[Optional[datetime], Optional[datetime]]]:
        """(last_down_at, up_since) from the log fetched by the last fetch().

        up_since is the recovery that ended the most recent outage -- how
        long the service has actually been continuously up, which is the
        number a locally-hosted monitor cannot know across its own restart.
        Returns None when there is no log to read yet.
        """
        if not self._last_logs:
            # No log at all. If the monitor is up and we know when it was
            # created, that is still a better floor than local tracking,
            # which resets every time this container restarts.
            if self._status is True and self._created_at:
                return None, self._created_at
            return None

        def stamp(entry: Dict[str, Any]) -> Optional[datetime]:
            try:
                return datetime.fromtimestamp(int(entry["datetime"]),
                                              tz=timezone.utc)
            except (TypeError, ValueError, KeyError):
                return None

        # The API returns newest first, but that is not promised anywhere,
        # so sort rather than trust it.
        entries = [e for e in self._last_logs if stamp(e)]
        entries.sort(key=lambda e: int(e["datetime"]), reverse=True)

        last_down = next((stamp(e) for e in entries
                          if e.get("type") == LOG_DOWN), None)
        up_since = None
        for entry in entries:
            if entry.get("type") == LOG_UP:
                candidate = stamp(entry)
                # Only counts as "up since" if nothing knocked it down
                # afterwards.
                if last_down is None or candidate > last_down:
                    up_since = candidate
                break

        if up_since is None and last_down is None:
            # The log holds only pause/start entries -- no real transition
            # either way. Fall back to creation for the same reason as above.
            if self._status is True and self._created_at:
                up_since = self._created_at
        return last_down, up_since

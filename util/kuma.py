"""Uptime Kuma 2.x client.

The original bot used the `uptime_kuma_api` package, which was last released
in September 2023 and only supports Uptime Kuma 1.23.x and earlier. It cannot
talk to a 2.x server, so this replaces it entirely.

Uptime Kuma 2.x still has no general REST API. What it does have:

  * GET /metrics -- Prometheus text, HTTP Basic auth (the API key goes in the
    password field; the username is ignored). Exposes `monitor_status` and
    `monitor_uptime_ratio` with windows 1d / 30d / 365d. The uptime ratios
    were added in 2.1.0 -- on 2.0.x you only get instantaneous status.

  * GET /api/badge/{id}/uptime/{duration} -- accepts arbitrary durations
    (7d, 12h, 2w ...) but returns an SVG and requires the monitor to be
    published on a status page. This is the only built-in way to get a
    7-day figure, since /metrics has no 7d window.

Anything not covered here (notably the exact last-down timestamp) is tracked
by the bot itself in ServerStateTracker, which is immune to both API drift
and Kuma's heartbeat retention window.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger("plexmonitor.kuma")

STATUS_NAMES = {0: "DOWN", 1: "UP", 2: "PENDING", 3: "MAINTENANCE"}

# Matches: metric_name{label="value",...} 12.34
_SAMPLE_RE = re.compile(r'^(?P<name>[a-zA-Z_:][\w:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[^\s]+)\s*$')
_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
_BADGE_PCT_RE = re.compile(r">\s*([0-9.]+)\s*%\s*<")


def _parse_prometheus(text: str):
    """Yield (metric_name, labels_dict, float_value) for each sample line.

    A tiny parser rather than a `prometheus_client` dependency -- we only need
    two metrics and the format is trivial.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue  # skips NaN/Inf lines
        labels = {}
        if match.group("labels"):
            labels = {k: v.replace('\\"', '"') for k, v in _LABEL_RE.findall(match.group("labels"))}
        yield match.group("name"), labels, value


class KumaClient:
    """Reads a single monitor's status and uptime figures from Uptime Kuma 2.x."""

    def __init__(self, base_url: str, monitor_id: str | int,
                 api_key: str = "", username: str = "", password: str = "",
                 timeout: float = 10.0, verify_ssl: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.monitor_id = str(monitor_id)
        self.api_key = api_key
        self.username = username
        self.password = password
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.verify_ssl = verify_ssl
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._badge_supported: Optional[bool] = None
        self._ratio_warned = False
        self._badge_failures = 0
        self._badge_failure_limit = 5

    def _auth(self) -> Optional[aiohttp.BasicAuth]:
        # With any API key configured in Kuma, /metrics authenticates on the
        # key alone and ignores the username. Without one, it falls back to
        # the normal Kuma login.
        if self.api_key:
            return aiohttp.BasicAuth("", self.api_key)
        if self.username:
            return aiohttp.BasicAuth(self.username, self.password)
        return None

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(ssl=None if self.verify_ssl else False)
                self._session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch(self) -> Dict[str, Any]:
        """Return status plus whatever uptime windows are available.

        Never raises -- a monitoring sidecar being down should degrade the
        dashboard, not break it. Failures surface as None values plus an
        "error" key.
        """
        result: Dict[str, Any] = {
            "status": None, "response_ms": None,
            "uptime_24h": None, "uptime_7d": None, "uptime_30d": None, "uptime_1y": None,
            "error": None,
        }

        try:
            await self._read_metrics(result)
        except Exception as exc:
            result["error"] = str(exc)
            logger.warning("Uptime Kuma /metrics failed: %s", exc)

        # /metrics has no 7-day window at all, so fill it from the badge
        # endpoint when the monitor is public. Skipped permanently after the
        # first failure so we stop paying for a request that cannot work.
        if self._badge_supported is not False:
            try:
                value = await self._read_badge("7d")
                if value is None:
                    # A definitive "N/A" badge means the monitor is not public.
                    # That will not fix itself, so stop asking.
                    self._badge_supported = False
                    logger.info(
                        "Uptime Kuma badge for monitor %s returned N/A -- the monitor is not "
                        "published on a status page, so 7-day uptime will be hidden.",
                        self.monitor_id,
                    )
                else:
                    result["uptime_7d"] = value
                    self._badge_supported = True
                    self._badge_failures = 0
            except Exception as exc:
                # A transport error is not evidence the endpoint cannot work --
                # Kuma restarting should not disable 7-day uptime until the bot
                # is restarted. Only give up after several consecutive failures.
                self._badge_failures += 1
                if self._badge_failures >= self._badge_failure_limit:
                    self._badge_supported = False
                    logger.info(
                        "Uptime Kuma badge endpoint failed %d times in a row (%s) -- "
                        "hiding 7-day uptime", self._badge_failures, exc,
                    )
                else:
                    logger.debug("Uptime Kuma badge fetch failed (%s), will retry", exc)

        return result

    async def _read_metrics(self, result: Dict[str, Any]) -> None:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/metrics", auth=self._auth()) as response:
            if response.status in (401, 403):
                raise RuntimeError(
                    "Uptime Kuma rejected the credentials for /metrics. Create an API key "
                    "under Settings -> API Keys and set UPTIME_API_KEY."
                )
            response.raise_for_status()
            text = await response.text()

        saw_ratio = False
        for name, labels, value in _parse_prometheus(text):
            if labels.get("monitor_id") != self.monitor_id:
                continue
            if name == "monitor_status":
                result["status"] = STATUS_NAMES.get(int(value), "UNKNOWN")
            elif name == "monitor_response_time":
                result["response_ms"] = value
            elif name == "monitor_uptime_ratio":
                saw_ratio = True
                window = labels.get("window")
                key = {"1d": "uptime_24h", "30d": "uptime_30d", "365d": "uptime_1y"}.get(window)
                if key:
                    result[key] = value * 100

        if result["status"] is None:
            raise RuntimeError(
                f"No monitor with id {self.monitor_id} found in Kuma's /metrics output. "
                f"Check UPTIME_MONITOR_ID."
            )
        if not saw_ratio and not self._ratio_warned:
            self._ratio_warned = True
            logger.warning(
                "Uptime Kuma exposed no monitor_uptime_ratio. That metric was added in 2.1.0 -- "
                "upgrade if you want uptime percentages."
            )

    async def _read_badge(self, duration: str) -> Optional[float]:
        """Uptime over an arbitrary window, scraped from the badge SVG.

        Unauthenticated, but Kuma answers with a grey 'N/A' badge instead of
        an error when the monitor is not public, so that case is detected
        rather than parsed.
        """
        session = await self._get_session()
        url = f"{self.base_url}/api/badge/{self.monitor_id}/uptime/{duration}"
        async with session.get(url) as response:
            response.raise_for_status()
            svg = await response.text()

        if "N/A" in svg:
            return None
        matches = _BADGE_PCT_RE.findall(svg)
        return float(matches[-1]) if matches else None

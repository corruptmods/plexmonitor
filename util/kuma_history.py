"""Uptime Kuma 2.x historical transition lookup, over socket.io.

Kuma's HTTP surface (/metrics, badges) exposes only current status and
uptime ratios -- no timestamps. The transition history lives behind the
socket.io API, and it is worth reaching for: Kuma prunes ordinary
heartbeats after 24 hours but keeps every *state transition* beat for
`keepDataPeriodDays`, which defaults to 365.

That means the answer to "when did Plex actually last go down" is already
recorded, and the bot's uptime counter can be seeded with real history
rather than starting from zero on first deploy.

Verified against Uptime Kuma 2.4.0:

  * `monitorImportantHeartbeatListPaged(monitorID, offset, count, ack)`
    returns {ok, data:[...]} newest-first, where each row carries
    monitorID, status, time, msg, ping, important, duration, retries.
  * `important = 1` marks transitions in BOTH directions, plus MAINTENANCE
    transitions and the monitor's very first beat -- so rows are filtered
    on `status` explicitly rather than assumed to alternate.
  * `heartbeat.time` is ALWAYS UTC, formatted "YYYY-MM-DD HH:mm:ss.SSS"
    with no timezone suffix. MariaDB truncates the milliseconds.

This module is optional. If python-socketio is not installed, or no
credentials are configured, the bot falls back to tracking transitions
itself and simply starts counting from first run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("plexmonitor.kuma_history")

try:
    import socketio  # type: ignore
    SOCKETIO_AVAILABLE = True
except ImportError:  # pragma: no cover
    socketio = None  # type: ignore
    SOCKETIO_AVAILABLE = False

DOWN, UP, PENDING, MAINTENANCE = 0, 1, 2, 3

# How many transition rows to pull. A monitor that flaps a lot could have
# many; 500 covers years of normal operation.
PAGE_SIZE = 500


def parse_kuma_time(value: Any) -> Optional[datetime]:
    """Parse a Kuma heartbeat timestamp into an aware UTC datetime.

    The string carries no timezone marker, so UTC must be attached
    explicitly -- letting fromisoformat() infer it would silently apply the
    container's local zone and skew every figure by the UTC offset.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)

    text = str(value).strip().replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.debug("Could not parse Kuma timestamp %r", value)
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class KumaHistoryClient:
    """Blocking socket.io lookup. Call via asyncio.to_thread."""

    def __init__(self, base_url: str, monitor_id: str | int,
                 username: str, password: str,
                 verify_ssl: bool = True, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.monitor_id = int(monitor_id)
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        # Kuma's login rate limiter is a single global bucket of 20/minute,
        # so the JWT is cached and reused rather than logging in each time.
        self._jwt: Optional[str] = None

    @property
    def available(self) -> bool:
        return SOCKETIO_AVAILABLE and bool(self.username and self.password)

    def fetch_transitions(self) -> Optional[Tuple[Optional[datetime], Optional[datetime]]]:
        """Return (last_down_at, up_since), or None if the lookup failed.

        up_since is the timestamp of the recovery transition following the
        most recent outage -- i.e. exactly how long the monitored service
        has been continuously up.
        """
        if not self.available:
            return None

        client = socketio.Client(
            reconnection=False,
            request_timeout=self.timeout,
            ssl_verify=self.verify_ssl,
        )
        try:
            # python-socketio sends no Origin header, which Kuma's
            # allowRequest hook explicitly permits.
            client.connect(
                self.base_url,
                transports=["websocket"],
                socketio_path="socket.io",
                wait_timeout=30,
            )
        except Exception as exc:
            logger.info("Could not open a socket.io connection to Uptime Kuma: %s", exc)
            return None

        try:
            if not self._authenticate(client):
                return None
            rows = self._fetch_rows(client)
            if rows is None:
                return None
            return self._derive(rows)
        except Exception as exc:
            logger.info("Uptime Kuma history lookup failed: %s", exc)
            return None
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    def _authenticate(self, client) -> bool:
        if self._jwt:
            try:
                response = client.call("loginByToken", self._jwt, timeout=self.timeout)
                if isinstance(response, dict) and response.get("ok"):
                    return True
            except Exception:
                pass
            self._jwt = None  # expired or rejected; fall through to a full login

        # No `token` field: 2FA can't be answered headlessly, and an
        # unnecessary token makes Kuma fire the login callback twice.
        payload: Dict[str, Any] = {"username": self.username, "password": self.password}
        response = client.call("login", payload, timeout=self.timeout)
        if not isinstance(response, dict):
            logger.info("Unexpected Uptime Kuma login response")
            return False

        # {tokenRequired: true} carries no "ok" key, so check it first.
        if response.get("tokenRequired"):
            logger.warning(
                "Uptime Kuma account has 2FA enabled. Historical uptime cannot be seeded "
                "without a TOTP code -- the bot will track uptime itself instead."
            )
            return False
        if not response.get("ok"):
            logger.warning("Uptime Kuma login rejected: %s", response.get("msg"))
            return False

        self._jwt = response.get("token")
        return True

    def _fetch_rows(self, client) -> Optional[List[Dict[str, Any]]]:
        response = client.call(
            "monitorImportantHeartbeatListPaged",
            (self.monitor_id, 0, PAGE_SIZE),
            timeout=self.timeout,
        )
        if not isinstance(response, dict) or not response.get("ok"):
            logger.info(
                "Uptime Kuma rejected the history query: %s",
                (response or {}).get("msg") if isinstance(response, dict) else response,
            )
            return None
        return response.get("data") or []

    @staticmethod
    def _derive(rows: List[Dict[str, Any]]) -> Tuple[Optional[datetime], Optional[datetime]]:
        """Walk newest-first to the most recent DOWN transition.

        The UP transition seen on the way there is the recovery that ended
        that outage, and therefore the true start of the current uptime.
        """
        last_down: Optional[datetime] = None
        up_since: Optional[datetime] = None

        for row in rows:
            try:
                status = int(row.get("status"))
            except (TypeError, ValueError):
                continue
            if status == DOWN:
                last_down = parse_kuma_time(row.get("time"))
                break
            if status == UP and up_since is None:
                up_since = parse_kuma_time(row.get("time"))

        return last_down, up_since

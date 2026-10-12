"""Persisted server up/down state.

Neither Plex nor Tautulli exposes a server uptime value -- Plex simply does
not publish one, and a grep of the Tautulli source turns up nothing. The
original bot worked around this by starting a timer the first time it
connected, which meant restarting the container reset "uptime" to zero even
though Plex had never gone down.

This tracks transitions itself and writes them to disk, so the figure
survives bot restarts, container recreation and image rebuilds. The state
file lives in data/, which is already a mounted volume.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("plexmonitor.state")


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class ServerStateTracker:
    """Tracks when the server last came up and last went down."""

    def __init__(self, path: str, down_confirmations: int = 2) -> None:
        self.path = path
        # Consecutive failed polls required before recording an outage.
        self.down_confirmations = max(1, int(down_confirmations))
        self._offline_strikes = 0
        self.up_since: Optional[datetime] = None
        self.down_since: Optional[datetime] = None
        self.last_down: Optional[datetime] = None
        self.last_status: Optional[bool] = None
        self._load()

    # ------------------------------------------------------------------ disk

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:     # ValueError: JSON + encoding
            logger.warning("Could not read state file %s (%s) -- starting fresh", self.path, exc)
            return
        if not isinstance(data, dict):
            logger.warning("State file %s is not a JSON object -- starting "
                           "fresh", self.path)
            return

        self.up_since = _parse(data.get("up_since"))
        self.down_since = _parse(data.get("down_since"))
        self.last_down = _parse(data.get("last_down"))
        status = data.get("last_status")
        self.last_status = None if status is None else bool(status)
        logger.info(
            "Restored server state: up_since=%s last_down=%s",
            self.up_since.isoformat() if self.up_since else None,
            self.last_down.isoformat() if self.last_down else None,
        )

    def _save(self) -> None:
        payload = {
            "up_since": self.up_since.isoformat() if self.up_since else None,
            "down_since": self.down_since.isoformat() if self.down_since else None,
            "last_down": self.last_down.isoformat() if self.last_down else None,
            "last_status": self.last_status,
        }
        # Write to a temp file in the same directory then rename, so a crash
        # mid-write cannot leave a truncated state file behind.
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        handle = None
        try:
            os.makedirs(directory, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
            )
            try:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(handle.name, self.path)
        except OSError as exc:
            logger.error("Could not write state file %s: %s", self.path, exc)
            # Disk full / read-only: don't leave tmp*.tmp litter behind.
            if handle is not None:
                try:
                    os.remove(handle.name)
                except OSError:
                    pass

    # --------------------------------------------------------------- updating

    def update(self, online: bool, now: Optional[datetime] = None) -> None:
        """Feed the current reachability; records transitions only.

        Going down is debounced: a single failed poll does not wipe the uptime
        counter, because one slow response from Tautulli should not reset a
        60-day figure. Coming back up is recorded immediately.
        """
        now = now or datetime.now(timezone.utc)

        if online:
            self._offline_strikes = 0
            # The `up_since is None` clause repairs a state file that says the
            # server is up but has no start time -- without it, uptime would
            # read "Unknown" forever.
            if self.last_status is not True or self.up_since is None:
                if self.last_status is False:
                    logger.info("Server came back online")
                if self.up_since is None:
                    self.up_since = now
                self.down_since = None
                self.last_status = True
                self._save()
            return

        if self.last_status is False:
            return  # already recorded as down

        self._offline_strikes += 1
        if self._offline_strikes < self.down_confirmations:
            logger.debug(
                "Server looked offline (%d/%d) -- waiting for confirmation",
                self._offline_strikes, self.down_confirmations,
            )
            return

        logger.info("Server went offline")
        self.last_down = now
        self.down_since = now
        self.up_since = None
        self.last_status = False
        self._save()

    def seed(self, up_since: Optional[datetime], last_down: Optional[datetime]) -> bool:
        """Apply authoritative history from an external monitor.

        Uptime Kuma has been watching the server far longer than this bot
        has, and keeps transition beats for a year, so its record beats
        anything tracked locally. Returns True if anything changed.
        """
        changed = False

        if last_down and last_down != self.last_down:
            self.last_down = last_down
            changed = True

        # Only trust an up_since that is actually in the past. A clock skew
        # between Kuma and this container could otherwise produce a negative
        # uptime that renders as "0s" forever.
        now = datetime.now(timezone.utc)
        if up_since and up_since <= now and up_since != self.up_since:
            previous = self.up_since
            self.up_since = up_since
            changed = True
            if previous is None:
                logger.info(
                    "Seeded uptime from Uptime Kuma history: up since %s",
                    up_since.isoformat(),
                )
            else:
                logger.info(
                    "Corrected uptime start from %s to %s using Uptime Kuma history",
                    previous.isoformat(), up_since.isoformat(),
                )

        if changed:
            if self.last_status is None:
                self.last_status = True
            self._save()
        return changed

    # --------------------------------------------------------------- querying

    def uptime_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        if not self.up_since:
            return None
        return ((now or datetime.now(timezone.utc)) - self.up_since).total_seconds()

    def downtime_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        if not self.down_since:
            return None
        return ((now or datetime.now(timezone.utc)) - self.down_since).total_seconds()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "up_since": self.up_since,
            "down_since": self.down_since,
            "last_down": self.last_down,
            "online": self.last_status,
        }

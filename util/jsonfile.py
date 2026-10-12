"""Small JSON state files in data/ -- read defensively, written atomically.

Temp file + rename, so a crash mid-write never leaves a truncated file: a
truncated state file would load as empty and, for example, re-arm request
announcements or reset today's AI question counts.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("plexmonitor.jsonfile")


def read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as exc:
        logger.error("Unreadable JSON in %s: %s -- using defaults", path, exc)
        return default


def write_json(path: str, payload: Any) -> bool:
    """Write atomically. False (and a warning) when the file couldn't be
    written -- callers that tell someone "saved" must check it."""
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def data_dir() -> str:
    """Where the bot keeps its state: PLEXMONITOR_DATA_DIR, else data/ next
    to main.py."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.getenv("PLEXMONITOR_DATA_DIR") or os.path.join(base, "data")

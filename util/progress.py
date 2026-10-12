"""Config-driven progress bar rendering.

Every bar in the bot -- Plex streams, SABnzbd downloads, anything added later --
goes through ProgressBar.render() so they always look identical.

Styles live in data/config.json under "progress_bar". Switching between
character sets, changing the length, toggling the inline-code backticks, or
moving to custom Discord emoji are all config edits; none of them need a
code change.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger("plexmonitor.progress")

# Shipped character sets. Users can add their own in config.json -- anything
# defined there is merged over these, so a config entry with the same name wins.
BUILTIN_STYLES: Dict[str, Dict[str, str]] = {
    "parallelogram": {"filled": "▰", "empty": "▱"},  # ▰ ▱
    "block":         {"filled": "█", "empty": "░"},  # █ ░
    "shade":         {"filled": "▓", "empty": "░"},  # ▓ ░
    "vertical":      {"filled": "▮", "empty": "▯"},  # ▮ ▯
    "line":          {"filled": "━", "empty": "─"},  # ━ ─
    "square":        {"filled": "■", "empty": "□"},  # ■ □
    "circle":        {"filled": "⬤", "empty": "◯"},  # ⬤ ◯
    "ascii":         {"filled": "=",      "empty": "-"},
}

DEFAULT_STYLE = "parallelogram"
MIN_SEGMENTS = 4
MAX_SEGMENTS = 60

# A style is treated as emoji-based if its glyphs look like Discord custom
# emoji (<:name:id> or <a:name:id>). Those cannot go inside backticks --
# Discord renders the raw text instead of the image -- so inline_code is
# force-disabled for them.
_EMOJI_PREFIXES = ("<:", "<a:")


class ProgressBar:
    """Renders percentages as bars according to the "progress_bar" config."""

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})

        # Merge user-defined styles over the built-ins.
        styles = dict(BUILTIN_STYLES)
        for name, spec in (cfg.get("styles") or {}).items():
            if isinstance(spec, dict) and spec.get("filled") and spec.get("empty"):
                styles[name] = spec
            else:
                logger.warning(
                    "progress_bar.styles.%s is missing 'filled' and/or 'empty' -- ignoring it",
                    name,
                )
        self.styles = styles

        # Resolve the selected style, falling back loudly rather than raising
        # inside the dashboard loop.
        name = cfg.get("style", DEFAULT_STYLE)
        if name not in self.styles:
            logger.warning(
                "progress_bar.style %r is not defined (known: %s) -- falling back to %r",
                name, ", ".join(sorted(self.styles)), DEFAULT_STYLE,
            )
            name = DEFAULT_STYLE
        self.style_name = name
        self.style = self.styles[name]

        self.filled = self.style["filled"]
        self.empty = self.style["empty"]
        # Caps come in two states so a rounded bar reads correctly at the
        # ends: the left cap is only "filled" once progress has started, and
        # the right cap only at a true 100%. A style may supply just
        # cap_left/cap_right, in which case both states are that one glyph
        # -- which is what every character style wants.
        self.cap_left = self.style.get("cap_left", "")
        self.cap_right = self.style.get("cap_right", "")
        self.cap_left_empty = self.style.get("cap_left_empty", self.cap_left)
        self.cap_right_full = self.style.get("cap_right_full", self.cap_right)

        # Clamp segment count into something that actually fits a Discord embed.
        segments = cfg.get("segments", 10)
        try:
            segments = int(segments)
        except (TypeError, ValueError):
            logger.warning("progress_bar.segments %r is not a number -- using 10", segments)
            segments = 10
        if not MIN_SEGMENTS <= segments <= MAX_SEGMENTS:
            logger.warning(
                "progress_bar.segments %d is outside %d-%d -- clamping",
                segments, MIN_SEGMENTS, MAX_SEGMENTS,
            )
            segments = max(MIN_SEGMENTS, min(MAX_SEGMENTS, segments))
        self.segments = segments

        self.show_percent = bool(cfg.get("show_percent", True))
        # Never show a completely empty bar for a stream that is actually
        # playing. Beyond reading better, it removes the widest state the
        # bar can take: with glyphs whose filled and empty forms are not
        # the same width, all-empty is the case that overflows a narrow
        # column, and it is the one state this makes unreachable.
        try:
            self.min_filled = int(cfg.get("min_filled", 1))
        except (TypeError, ValueError):
            self.min_filled = 1
        self.min_filled = max(0, min(self.segments, self.min_filled))

        # Emoji bars cannot be wrapped in backticks.
        self.is_emoji = str(self.filled).startswith(_EMOJI_PREFIXES)
        requested_inline = bool(cfg.get("inline_code", True))
        if self.is_emoji and requested_inline:
            logger.warning(
                "progress_bar.style %r uses custom emoji, which Discord will not render "
                "inside inline code -- ignoring progress_bar.inline_code=true",
                self.style_name,
            )
        self.inline_code = requested_inline and not self.is_emoji

    def render(self, percent: float, show_percent: bool | None = None,
               inline: bool | None = None) -> str:
        """Render `percent` (0-100) as a bar string ready to drop in an embed.

        `inline` overrides the configured inline_code setting. Pass False when
        the bar is going inside a fenced code block, which is already
        monospace -- nested backticks would just print literally.
        """
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            pct = 0.0
        pct = max(0.0, min(100.0, pct))

        # int() rather than round() so a bar only shows "full" at a true 100%.
        n = int(pct / 100 * self.segments)
        if pct >= 100:
            n = self.segments
        elif pct > 0 or self.min_filled:
            n = max(n, self.min_filled)

        left = self.cap_left if n > 0 else self.cap_left_empty
        right = self.cap_right_full if n >= self.segments else self.cap_right
        bar = (f"{left}{self.filled * n}"
               f"{self.empty * (self.segments - n)}{right}")
        want_inline = self.inline_code if inline is None else (inline and not self.is_emoji)
        if want_inline:
            bar = f"`{bar}`"

        want_percent = self.show_percent if show_percent is None else show_percent
        if not want_percent:
            return bar
        # Truncate rather than round, so 99.6% never prints "100%" next to a
        # bar that is visibly not full.
        return f"{bar} {self.display_percent(pct)}%"

    def display_percent(self, percent: float) -> int:
        """The percent to print, floored to match a floored bar.

        A bar showing one filled segment beside a bold "0%" reads as a bug,
        so the number follows the bar rather than the raw value.
        """
        try:
            pct = max(0.0, min(100.0, float(percent)))
        except (TypeError, ValueError):
            pct = 0.0
        if pct >= 100:
            return 100
        shown = int(pct)
        if self.min_filled and shown < 1:
            shown = 1
        return shown

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ProgressBar style={self.style_name!r} segments={self.segments} "
            f"inline_code={self.inline_code} emoji={self.is_emoji}>"
        )

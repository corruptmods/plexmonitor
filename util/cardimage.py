"""Rendered card graphics.

The width-equalization endgame: Discord sizes an embed to its image, and an
image has an exact pixel width. Every card carries a rendered progress bar
at the same fixed width, and the dashboard carries an accent strip at that
width -- so every embed in the message is pixel-identical in width, on
every client, with no font arithmetic involved.

Unlike the invisible spacer experiment, these images ARE the content: the
bar replaces the text progress line rather than adding dead space.
"""

from __future__ import annotations

import io
from typing import Tuple

try:
    from PIL import Image, ImageDraw
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

# One width to rule them all. 1024 comfortably exceeds Discord's maximum
# embed width, so every embed renders at the client's cap -- the same cap.
WIDTH = 1024
BAR_HEIGHT = 44
STRIP_HEIGHT = 14

TRACK = (67, 68, 75, 255)          # Discord's muted grey


def _rgb(colour: int) -> Tuple[int, int, int]:
    return ((colour >> 16) & 0xFF, (colour >> 8) & 0xFF, colour & 0xFF)


def bar_png(percent: float, colour: int) -> bytes:
    """A rounded progress bar, track plus fill, transparent background."""
    pct = max(0.0, min(100.0, float(percent)))
    img = Image.new("RGBA", (WIDTH, BAR_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = BAR_HEIGHT // 2

    draw.rounded_rectangle([0, 0, WIDTH - 1, BAR_HEIGHT - 1],
                           radius=radius, fill=TRACK)

    fill = _rgb(colour) + (255,)
    fill_width = int(WIDTH * pct / 100)
    if fill_width >= BAR_HEIGHT:
        draw.rounded_rectangle([0, 0, fill_width - 1, BAR_HEIGHT - 1],
                               radius=radius, fill=fill)
    elif pct > 0:
        # Below one radius of progress, draw the minimal round nub.
        draw.ellipse([0, 0, BAR_HEIGHT - 1, BAR_HEIGHT - 1], fill=fill)

    buffer = io.BytesIO()
    img.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def strip_png(colour: int) -> bytes:
    """A slim accent strip for the dashboard, width-matched to the bars.

    The line is drawn at the TOP of the canvas rather than centred in it.
    Discord puts a fixed margin above every embed image and that cannot be
    reduced, so the only way to close the gap between the strip and the
    text above it is to stop wasting the image's own top padding.
    """
    img = Image.new("RGBA", (WIDTH, STRIP_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    inset = 0
    draw.rounded_rectangle(
        [0, inset, WIDTH - 1, (STRIP_HEIGHT // 3) - 1],
        radius=(STRIP_HEIGHT - 2 * inset) // 2,
        fill=_rgb(colour) + (110,),
    )
    buffer = io.BytesIO()
    img.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def rule_png(colour: int) -> bytes:
    """A width anchor you can actually see: a hairline rule.

    Same job as anchor_png -- be exactly WIDTH wide so the embed renders at
    the client's maximum width -- but it fills Discord's image margin with
    a deliberate divider instead of dead space. Tinted to the card's own
    status colour so it echoes the coloured edge on the left.
    """
    img = Image.new("RGBA", (WIDTH, 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 1, WIDTH - 1, 2], radius=1,
                           fill=_rgb(colour) + (90,))
    buffer = io.BytesIO()
    img.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def anchor_png() -> bytes:
    """A fully transparent width anchor.

    Carries no pixels anyone can see -- its only job is to be exactly WIDTH
    wide, which forces Discord to render the embed at the client's maximum
    width. Every card gets one, so every card matches, the same way the
    rendered bars used to equalise them. The content never varies, so it
    uploads once in the message's lifetime and is adopted by URL forever
    after.
    """
    img = Image.new("RGBA", (WIDTH, 2), (0, 0, 0, 0))
    buffer = io.BytesIO()
    img.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()

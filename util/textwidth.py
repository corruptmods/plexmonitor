"""Pixel-accurate text measurement for Discord embed layout.

Character COUNT is meaningless for embed width: "WWWW" is four times the
width of "iiii" in Discord's proportional UI font, so a packer that budgets
by length produces cards of wildly different widths. This module measures
instead, so every card can be pinned to the same rendered width without an
image doing it for us.

Widths come from Liberation Sans (metric-compatible with Arial, and close
to Discord's gg sans), tabulated at em=1000. They are approximate by
construction -- the goal is not to predict Discord's layout exactly, but to
guarantee an upper bound, so a line trimmed to fit is genuinely narrower
than the monospace bar that sets the card width.
"""

from __future__ import annotations

from typing import Iterable, List

# Discord's rendered sizes.
BODY_PX = 16.0          # embed description text
SUBTEXT_PX = 12.8       # "-# " lines, 0.8 of body
HEADING_PX = 20.0       # "### " lines, the smallest heading
BOLD_RATIO = 1.068      # Liberation Sans bold vs regular, averaged
# Consolas/Menlo inside inline code: 14px at 0.55em advance.
MONO_ADVANCE = 7.7
# Inline code adds ~3px padding plus a 1px border on each side.
CODE_CHROME = 8.0
# Anything not in the table -- emoji, CJK, box drawing. Emoji render about
# 1.15em in practice; erring wide keeps the bound safe.
UNKNOWN = 1150
# A custom emoji in body text renders at roughly this width, regardless of
# the surrounding font size -- Discord draws it at a fixed box.
EMOJI_PX = 22.0

_TABLE = {}
for _pair in r""" 278!278"355#556$556%889&667'191(333)333*389+584,278-333.278/2780556155625563556455655566556755685569556:278;278<584=584>584?556@1015A667B667C722D722E667F611G778H722I278J500K667L556M833N722O778P667Q778R722S667T611U722V667W944X667Y667Z611[278\278]278^469_556`333a556b556c500d556e556f278g556h556i222j222k500l222m833n556o556p556q556r333s500t278u556v500w722x500y500z500{334|260}334~584·333–556—1000‘222’222“333”333…1000→1000█708░708▰750▱750""".split(chr(2)):
    if _pair:
        _c, _v = _pair.split(chr(1))
        _TABLE[_c] = int(_v)

# U+2007 FIGURE SPACE is defined by Unicode as the width of a digit, and is
# what the progress bar pads its percent with so "8%", "80%" and "100%" all
# measure the same. Absent from the table it fell through to UNKNOWN (1150),
# which is nearly double a digit -- so the padded percents measured WIDER
# than the unpadded one and the fit maths ran backwards.
_TABLE[" "] = _TABLE["0"]
# U+2008 PUNCTUATION SPACE and U+2009 THIN SPACE, for the same reason.
_TABLE.setdefault(" ", _TABLE["."])
_TABLE.setdefault(" ", 200)


import re as _re

# Discord renders these as something quite unlike their source text, so
# measuring the raw markup is meaningless. Substitute a representative
# rendering before measuring, erring on the long side.
_TIMESTAMP = _re.compile(r"<t:\d+:[a-zA-Z]?>")
_CUSTOM_EMOJI = _re.compile(r"<a?:\w+:\d+>")
# "in about 2 hours" is roughly the longest relative form in normal use.
_TIMESTAMP_AS = "in about 2 hours"


def rendered(text: str) -> str:
    """What Discord actually draws, for measurement purposes."""
    text = _TIMESTAMP.sub(_TIMESTAMP_AS, text)
    text = _CUSTOM_EMOJI.sub("\U0001f600", text)
    return text.replace("**", "").replace("`", "").replace("*", "")


def atomic(text: str) -> bool:
    """True if cutting this token would corrupt it rather than shorten it.

    A half-written <t:...:R> does not render as a shorter timestamp -- it
    renders as literal garbage, which is exactly what the first version of
    this packer produced.
    """
    return "<" in text and ">" in text


def px(text: str, size: float = BODY_PX, bold: bool = False) -> float:
    """Rendered width of `text` in pixels, ignoring markdown syntax."""
    total = sum(_TABLE.get(ch, UNKNOWN) for ch in rendered(text))
    out = total * size / 1000.0
    return out * BOLD_RATIO if bold else out


def bar_px(segments: int, suffix: str = "", emoji: bool = False,
           caps: int = 0) -> float:
    """Width of a rendered progress bar line.

    Monospace bars sit in a code span; emoji bars do not (Discord will not
    render custom emoji inside backticks) and each glyph is a fixed ~22px
    box rather than a 7.7px cell -- nearly 3x wider, so a budget computed
    the monospace way would be far too small and text would end up setting
    the card width instead of the bar.
    """
    if emoji:
        return (segments + caps) * EMOJI_PX + px(suffix)
    return segments * MONO_ADVANCE + CODE_CHROME + px(suffix)


def code_px(text: str) -> float:
    """Width of `text` in an inline code span: monospace cells plus the
    span's own padding and border."""
    return len(text) * MONO_ADVANCE + CODE_CHROME


def truncate(text: str, budget: float, size: float = BODY_PX,
             bold: bool = False, ellipsis: str = "\u2026") -> str:
    """Trim `text` until it fits `budget` pixels, appending an ellipsis."""
    if px(text, size, bold) <= budget or atomic(text):
        return text
    tail = px(ellipsis, size, bold)
    kept: List[str] = []
    used = 0.0
    for ch in text:
        step = px(ch, size, bold)
        if used + step + tail > budget:
            break
        kept.append(ch)
        used += step
    return "".join(kept).rstrip() + ellipsis


def fit_wrap(text: str, budget: float, size: float = BODY_PX,
             bold: bool = False) -> str:
    """Make `text` safe to wrap inside `budget` without widening the box.

    Proportional text wraps, so a long title does NOT force an embed wider
    -- only a single unbreakable WORD wider than the budget can do that.
    So trim the rare oversized word and leave everything else whole. Much
    better than truncating the whole string: the title keeps its meaning
    and simply takes two lines.
    """
    out = []
    for word in text.split(" "):
        out.append(word if px(word, size, bold) <= budget
                   else truncate(word, budget, size, bold))
    return " ".join(out)


def pack(tokens: Iterable[str], budget: float, separator: str = " \u00b7 ",
         size: float = BODY_PX) -> List[str]:
    """Greedily group tokens into lines that each fit `budget` pixels."""
    lines: List[str] = []
    current = ""
    sep_w = px(separator, size)
    for token in tokens:
        token = str(token)
        if not token:
            continue
        if not current:
            current = truncate(token, budget, size)
            continue
        if px(current, size) + sep_w + px(token, size) <= budget:
            current += separator + token
        else:
            lines.append(current)
            current = truncate(token, budget, size)
    if current:
        lines.append(current)
    return lines

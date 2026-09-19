"""Official startup banner rendering.

``assets/banner.png`` is the primary banner. It is rendered *inside the
terminal* with Pillow + Rich using the half-block technique: every character
cell carries two vertical pixels (upper half as the foreground colour of ``▀``,
lower half as the background colour), which doubles vertical resolution and
preserves the original aspect ratio without recolouring the artwork.

If anything at all goes wrong -- Pillow missing, file missing, corrupt image, a
terminal without truecolor -- :func:`render_banner` silently returns the ASCII
fallback from ``assets/banner_ascii.txt``. The user never has to intervene.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from rich.align import Align
from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from nextron.core import constants
from nextron.storage import paths

log = logging.getLogger(__name__)

__all__ = [
    "BannerRender",
    "PNGBanner",
    "ascii_banner",
    "png_supported",
    "render_banner",
]

#: Terminals known to reliably render 24-bit colour half blocks.
_TRUECOLOR_HINTS = ("truecolor", "24bit")

_HALF_BLOCK = "▀"
#: Pixels darker than this on every channel are treated as terminal background.
_TRANSPARENT_LUMA = 8


@dataclass(frozen=True, slots=True)
class BannerRender:
    """The result of a banner render request."""

    renderable: object
    is_png: bool
    reason: str = ""

    @property
    def kind(self) -> str:
        return "PNG" if self.is_png else "ASCII"


def png_supported() -> bool:
    """True when the environment can plausibly render the PNG banner."""
    if os.environ.get("NEXTRON_FORCE_ASCII"):
        return False
    try:
        import PIL  # noqa: F401
    except ImportError:
        log.debug("Pillow unavailable; PNG banner disabled")
        return False
    if not paths.banner_png().is_file():
        log.debug("Banner asset missing at %s", paths.banner_png())
        return False

    colorterm = os.environ.get("COLORTERM", "").lower()
    if any(hint in colorterm for hint in _TRUECOLOR_HINTS):
        return True
    term = os.environ.get("TERM", "").lower()
    # Textual always drives a truecolor-capable renderer; a bare "dumb"
    # terminal is the only case worth refusing outright.
    return term not in {"dumb", ""} or bool(os.environ.get("TEXTUAL"))


class PNGBanner:
    """A Rich renderable that draws a PNG with half-block characters."""

    def __init__(
        self,
        path=None,
        *,
        width: int = 72,
        max_height: int | None = None,
    ) -> None:
        self.path = path or paths.banner_png()
        self.width = max(8, int(width))
        self.max_height = max_height

    # -- rendering ---------------------------------------------------------- #

    def _load(self, width: int):
        from PIL import Image

        image = Image.open(self.path)
        image = image.convert("RGBA")

        # Two pixel rows per text row, and terminal cells are ~2x taller than
        # wide, so the aspect correction factor is 1.0 in half-block mode.
        source_w, source_h = image.size
        target_w = width
        target_h = max(2, round(source_h * (target_w / source_w)))
        if target_h % 2:
            target_h += 1

        if self.max_height is not None:
            limit = max(2, self.max_height * 2)
            if target_h > limit:
                scale = limit / target_h
                target_h = limit
                target_w = max(8, round(target_w * scale))
                if target_w % 2:
                    target_w -= 1

        return image.resize((target_w, target_h), Image.LANCZOS)

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = min(self.width, options.max_width)
        image = self._load(width)
        pixels = image.load()
        img_w, img_h = image.size

        for row in range(0, img_h, 2):
            segments: list[Segment] = []
            for column in range(img_w):
                top = pixels[column, row]
                bottom = pixels[column, row + 1] if row + 1 < img_h else top
                segments.append(_half_block_segment(top, bottom))
            yield from _compress(segments)
            yield Segment.line()


def _is_background(pixel: tuple[int, int, int, int]) -> bool:
    red, green, blue, alpha = pixel
    if alpha < 32:
        return True
    return max(red, green, blue) <= _TRANSPARENT_LUMA


def _half_block_segment(
    top: tuple[int, int, int, int], bottom: tuple[int, int, int, int]
) -> Segment:
    """Encode two stacked pixels into one character cell."""
    top_bg = _is_background(top)
    bottom_bg = _is_background(bottom)

    if top_bg and bottom_bg:
        return Segment(" ")
    if top_bg:
        # Only the lower pixel is painted: use a lower half block.
        return Segment("▄", Style(color=_hex(bottom)))
    if bottom_bg:
        return Segment(_HALF_BLOCK, Style(color=_hex(top)))
    return Segment(_HALF_BLOCK, Style(color=_hex(top), bgcolor=_hex(bottom)))


def _hex(pixel: tuple[int, int, int, int]) -> str:
    red, green, blue, _ = pixel
    return f"#{red:02x}{green:02x}{blue:02x}"


def _compress(segments: list[Segment]):
    """Merge runs of identical styles so Rich emits far fewer escape codes."""
    if not segments:
        return
    buffer = segments[0].text
    style = segments[0].style
    for segment in segments[1:]:
        if segment.style == style:
            buffer += segment.text
        else:
            yield Segment(buffer, style)
            buffer = segment.text
            style = segment.style
    yield Segment(buffer, style)


def ascii_banner(*, styled: bool = True) -> Text:
    """Load the ASCII fallback banner, styled in the brand palette."""
    art = _ascii_source()
    if not styled:
        return Text(art)

    text = Text(no_wrap=True, overflow="crop")
    lines = art.splitlines()
    for index, line in enumerate(lines):
        if constants.TAGLINE.split()[0] in line and index >= len(lines) - 2:
            text.append(line + "\n", style=f"italic {constants.COLOR_SURFACE}")
        else:
            text.append(line + "\n", style=f"bold {constants.COLOR_ACCENT}")
    return text


def _ascii_source() -> str:
    path = paths.banner_ascii()
    try:
        return path.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        log.debug("ASCII banner asset unreadable at %s", path)
        return _EMBEDDED_ASCII


def render_banner(
    *,
    width: int = 72,
    max_height: int | None = None,
    prefer_png: bool = True,
    center: bool = True,
) -> BannerRender:
    """Return the banner to display, falling back to ASCII automatically."""
    if prefer_png and png_supported():
        try:
            banner = PNGBanner(width=width, max_height=max_height)
            banner._load(width)  # fail fast here, not mid-render
            renderable = Align.center(banner) if center else banner
            return BannerRender(renderable=renderable, is_png=True)
        except Exception as exc:  # corrupt image, OOM, missing decoder...
            log.warning("PNG banner render failed (%s); using ASCII fallback", exc)
            reason = str(exc)
    else:
        reason = "PNG rendering unavailable"

    art = ascii_banner()
    renderable = Align.center(art) if center else art
    return BannerRender(renderable=renderable, is_png=False, reason=reason)


#: Last-resort copy of the fallback art, used only if the asset file is gone.
_EMBEDDED_ASCII = r"""
    )
 ( /(                )
 )\())   (     )  ( /( (
((_)\   ))\ ( /(  )\()))(    (    (
 _((_) /((_))\())(_))/(()\   )\   )\ )
| \| |(_)) ((_)\ | |_  ((_) ((_) _(_/(
| .` |/ -_)\ \ / |  _|| '_|/ _ \| ' \))
|_|\_|\___|/_\_\  \__||_|  \___/|_||_|

     Intelligent Tor Switcher with VPN & Ad Blocking in CLI
""".strip("\n")

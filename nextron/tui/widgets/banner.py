"""The startup banner widget.

Renders ``assets/banner.png`` with Pillow + Rich inside the terminal, centred
and aspect-correct, and silently falls back to the ASCII art from
``assets/banner_ascii.txt`` if anything prevents the image from rendering.
"""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static

from nextron.utils.banner import render_banner

__all__ = ["BannerWidget"]


class BannerWidget(Static):
    """A self-resizing banner."""

    #: Set to ``False`` to force the ASCII fallback (Settings → interface).
    prefer_png: reactive[bool] = reactive(True)
    #: Upper bound in cells; the banner never exceeds the available width.
    max_width: reactive[int] = reactive(96)
    #: Upper bound in text rows, so the banner cannot push the UI off-screen.
    max_rows: reactive[int | None] = reactive(None)

    def __init__(
        self,
        *,
        prefer_png: bool = True,
        max_width: int = 96,
        max_rows: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__("", **kwargs)
        self.set_reactive(BannerWidget.prefer_png, prefer_png)
        self.set_reactive(BannerWidget.max_width, max_width)
        self.set_reactive(BannerWidget.max_rows, max_rows)
        self._kind = "ASCII"

    @property
    def kind(self) -> str:
        """``"PNG"`` or ``"ASCII"`` -- which path actually rendered."""
        return self._kind

    def on_mount(self) -> None:
        self.redraw()

    def on_resize(self) -> None:
        self.redraw()

    def watch_prefer_png(self) -> None:
        if self.is_mounted:
            self.redraw()

    def redraw(self) -> None:
        """Render the banner at the current widget width."""
        available = self.size.width or self.max_width
        width = max(24, min(self.max_width, available))
        result = render_banner(
            width=width,
            max_height=self.max_rows,
            prefer_png=self.prefer_png,
            center=True,
        )
        self._kind = result.kind
        self.update(result.renderable)

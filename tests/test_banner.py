"""Banner rendering: the PNG path and the automatic ASCII fallback."""

from __future__ import annotations

from rich.console import Console

from nextron.core import constants
from nextron.storage import paths
from nextron.utils.banner import PNGBanner, ascii_banner, png_supported, render_banner


def _plain(renderable, width: int = 80) -> str:
    console = Console(width=width, force_terminal=True, color_system="truecolor")
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_png_banner_renders_when_the_asset_exists():
    assert paths.banner_png().is_file()
    result = render_banner(width=48, max_height=12)
    assert result.is_png is True
    assert result.kind == "PNG"
    output = _plain(result.renderable)
    # Half-block rendering: two pixel rows per text row.
    assert "▀" in output or "▄" in output


def test_png_render_respects_the_height_cap():
    banner = PNGBanner(width=40, max_height=6)
    image = banner._load(40)
    assert image.size[1] <= 12  # 6 text rows * 2 pixel rows


def test_ascii_fallback_is_used_when_png_is_forced_off():
    result = render_banner(prefer_png=False)
    assert result.is_png is False
    assert "NEXTRON" not in _plain(result.renderable)  # it is figlet-style art
    assert "Intelligent Tor Switcher" in _plain(result.renderable)


def test_environment_can_force_ascii(monkeypatch):
    monkeypatch.setenv("NEXTRON_FORCE_ASCII", "1")
    assert png_supported() is False
    assert render_banner().is_png is False


def test_missing_asset_falls_back_without_raising(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXTRON_ASSETS", str(tmp_path))
    assert png_supported() is False
    result = render_banner()
    assert result.is_png is False
    # The embedded copy keeps the tagline available even with no asset files.
    assert "Intelligent Tor Switcher" in _plain(result.renderable)


def test_corrupt_png_falls_back_automatically(monkeypatch, tmp_path):
    (tmp_path / "banner.png").write_bytes(b"this is not a png")
    (tmp_path / "banner_ascii.txt").write_text("FALLBACK ART\n", encoding="utf-8")
    monkeypatch.setenv("NEXTRON_ASSETS", str(tmp_path))

    result = render_banner()
    assert result.is_png is False
    assert result.reason  # the failure reason is reported, not swallowed
    assert "FALLBACK ART" in _plain(result.renderable)


def test_ascii_banner_uses_only_brand_colours():
    text = ascii_banner()
    styles = {str(span.style) for span in text.spans}
    for style in styles:
        for token in style.split():
            if token.startswith("#"):
                assert token.upper() in {
                    constants.COLOR_ACCENT.upper(),
                    constants.COLOR_SURFACE.upper(),
                }

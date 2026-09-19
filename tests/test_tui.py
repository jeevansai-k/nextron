"""Interface smoke tests driven through Textual's headless pilot."""

from __future__ import annotations

import pytest

from nextron.core import constants
from nextron.tui.app import NextronApp
from nextron.tui.screens.dashboard import DashboardScreen
from nextron.tui.screens.splash import SplashScreen
from tests.conftest import render_text

SIZE = (150, 46)


@pytest.fixture
async def app(profiles):
    instance = NextronApp()
    yield instance
    await instance.runtime.shutdown()


async def test_splash_shows_the_banner_then_the_dashboard(app):
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, SplashScreen)
        banner = app.screen.query_one("#splash-banner")
        assert banner.kind in {"PNG", "ASCII"}

        await pilot.press("escape")
        await pilot.pause(0.8)
        assert isinstance(app.screen, DashboardScreen)


@pytest.mark.parametrize(
    "key,screen_name",
    [
        ("m", "ModeSelectScreen"),
        ("v", "VPNLibraryScreen"),
        ("p", "RotationPoolScreen"),
        ("d", "DNSShieldScreen"),
        ("l", "LogsScreen"),
        ("s", "SettingsScreen"),
        ("a", "AboutScreen"),
        ("x", "DoctorScreen"),
    ],
)
async def test_every_menu_key_opens_its_screen_and_escapes_back(app, key, screen_name):
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.7)

        await pilot.press(key)
        await pilot.pause(0.4)
        assert type(app.screen).__name__ == screen_name

        await pilot.press("escape")
        await pilot.pause(0.3)
        assert isinstance(app.screen, DashboardScreen)


async def test_dashboard_shows_every_required_panel(app):
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)

        for panel_id in (
            "#panel-routing",
            "#panel-session",
            "#panel-countdown",
            "#panel-tor",
            "#panel-vpn",
            "#panel-dns",
            "#panel-activity",
        ):
            assert app.screen.query_one(panel_id) is not None


async def test_rotation_pool_screen_edits_the_configuration(app):
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)

        await pilot.press("p")
        await pilot.pause(0.4)
        before = app.runtime.config.vpn.shuffle_algorithm
        await pilot.press("g")          # cycle the shuffle algorithm
        await pilot.pause(0.3)
        assert app.runtime.config.vpn.shuffle_algorithm is not before

        await pilot.press("n")          # clear the pool
        await pilot.pause(0.3)
        assert app.runtime.config.vpn.shuffle_pool == []
        await pilot.press("a")          # select every profile
        await pilot.pause(0.3)
        assert len(app.runtime.config.vpn.shuffle_pool) == 3


async def test_tor_rotation_toggle_persists(app):
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)

        before = app.runtime.tor_scheduler.running
        await pilot.press("r")
        await pilot.pause(0.6)
        assert app.runtime.tor_scheduler.running is not before


async def test_about_screen_exposes_the_developer_information(app):
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("a")
        await pilot.pause(0.5)

        from textual.widgets import Static

        details = app.screen.query_one("#about-details", Static)
        rendered = str(details.content)
        assert constants.DEVELOPER in rendered
        assert constants.EMAIL in rendered
        assert "github.com/jeevansai-k" in rendered
        assert constants.LICENSE in rendered


async def test_quit_confirmation_can_be_cancelled(app):
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)

        await pilot.press("q")
        await pilot.pause(0.4)
        assert type(app.screen).__name__ == "ConfirmScreen"
        await pilot.press("n")
        await pilot.pause(0.4)
        assert isinstance(app.screen, DashboardScreen)
        assert app.is_running


# -- the beautified menu ----------------------------------------------------- #


async def test_menu_is_grouped_and_headers_are_skipped_by_navigation(app):
    from nextron.tui.widgets.menu import NavMenu

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)

        menu = app.screen.query_one(NavMenu)
        headers = [item for item in menu.children if item.disabled]
        actions = [item for item in menu.children if not item.disabled]
        assert len(headers) == 3          # Session, Libraries, System
        assert len(actions) == 11         # every action entry

        # The cursor starts on a real action, never a header.
        assert menu.index is not None
        assert not menu.children[menu.index].disabled

        # Walking down never lands on a header.
        for _ in range(len(actions) + 2):
            await pilot.press("down")
            await pilot.pause(0.05)
            assert not menu.children[menu.index].disabled


async def test_menu_shows_live_state(app):
    from nextron.tui.widgets.menu import NavMenu

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.9)

        menu = app.screen.query_one(NavMenu)
        assert "3 profiles" in render_text(menu._cells["vpn_library"].content)
        assert "Tor Only" in render_text(menu._cells["mode_select"].content)
        assert "disabled" in render_text(menu._cells["dns_shield"].content)


# -- the interval steppers --------------------------------------------------- #


async def test_dashboard_steps_the_tor_interval(app):
    from nextron.utils import intervals

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)

        app.runtime.set_tor_interval(60)
        await pilot.press("]")
        await pilot.pause(0.3)
        assert app.runtime.config.tor.rotation_interval == 90

        await pilot.press("[")
        await pilot.press("[")
        await pilot.pause(0.3)
        assert app.runtime.config.tor.rotation_interval == 30

        # It stops at the bottom rung instead of going below it.
        for _ in range(4):
            await pilot.press("[")
        await pilot.pause(0.3)
        assert app.runtime.config.tor.rotation_interval == intervals.PRESETS[0]


async def test_dashboard_steps_the_vpn_interval(app):
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)

        app.runtime.set_vpn_interval(120)
        await pilot.press(".")
        await pilot.pause(0.3)
        assert app.runtime.config.vpn.shuffle_interval == 150

        await pilot.press(",")
        await pilot.pause(0.3)
        assert app.runtime.config.vpn.shuffle_interval == 120


async def test_rotation_pool_steps_through_presets(app):
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("p")
        await pilot.pause(0.4)

        app.runtime.set_vpn_interval(30)
        await pilot.press("]")
        await pilot.pause(0.3)
        assert app.runtime.config.vpn.shuffle_interval == 60


async def test_settings_interval_cycles_and_wraps(app):
    from nextron.tui.screens.settings import SettingsScreen

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("s")
        await pilot.pause(0.4)

        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        # Headings share the row space with settings, so ask the screen.
        row = SettingsScreen.row_of("tor.rotation_interval")
        app.runtime.config.tor.rotation_interval = 300

        table = screen.query_one("#settings-table")
        table.move_cursor(row=row)
        await pilot.pause(0.2)
        await pilot.press("enter")
        await pilot.pause(0.3)
        # 5m is the top rung, so Enter wraps back to the shortest step.
        assert app.runtime.config.tor.rotation_interval == 15


# -- imports ----------------------------------------------------------------- #


async def test_vpn_import_falls_back_to_a_prompt_without_a_file_browser(
    app, monkeypatch
):
    monkeypatch.setenv("NEXTRON_NO_FILE_DIALOG", "1")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("v")
        await pilot.pause(0.4)

        await pilot.press("i")
        await pilot.pause(0.4)
        assert type(app.screen).__name__ == "PromptScreen"
        await pilot.press("escape")
        await pilot.pause(0.3)


async def test_vpn_import_accepts_several_typed_paths(app, tmp_path):
    first = tmp_path / "oslo.ovpn"
    second = tmp_path / "rome.ovpn"
    first.write_text("client\nproto tcp\nremote no.example.com 443\n", encoding="utf-8")
    second.write_text("client\nproto udp\nremote it.example.com 1194\n", encoding="utf-8")

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("v")
        await pilot.pause(0.4)

        before = len(app.runtime.vpn.library)
        app.screen._import_paths([first, second])
        await pilot.pause(0.4)
        assert len(app.runtime.vpn.library) == before + 2
        assert app.runtime.vpn.library.by_name("oslo") is not None
        assert app.runtime.vpn.library.by_name("rome") is not None


async def test_vpn_import_reports_a_bad_file_without_losing_the_good_one(
    app, tmp_path
):
    good = tmp_path / "good.ovpn"
    good.write_text("client\nproto tcp\nremote x.example.com 443\n", encoding="utf-8")
    bad = tmp_path / "bad.csv"
    bad.write_text("nope\n", encoding="utf-8")

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("v")
        await pilot.pause(0.4)

        app.screen._import_paths([bad, good])
        await pilot.pause(0.4)
        assert app.runtime.vpn.library.by_name("good") is not None


async def test_browsing_imports_every_selected_vpn_profile(app, tmp_path, monkeypatch):
    """The file browser path, with the chooser itself stubbed out."""
    from nextron.utils import filedialog

    first = tmp_path / "lisbon.ovpn"
    second = tmp_path / "madrid.wgconf"
    first.write_text("client\nproto tcp\nremote pt.example.com 443\n", encoding="utf-8")
    second.write_text(
        "[Interface]\nPrivateKey = a\nAddress = 10.9.0.2/32\n\n"
        "[Peer]\nPublicKey = b\nEndpoint = es.example.com:51820\n"
        "AllowedIPs = 0.0.0.0/0\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(filedialog, "available", lambda: "zenity")

    async def fake_pick(**kwargs):
        assert kwargs["multiple"] is True
        assert kwargs["file_filter"] is filedialog.VPN_FILTER
        return filedialog.DialogResult(paths=(first, second))

    monkeypatch.setattr(filedialog, "pick_files", fake_pick)

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("v")
        await pilot.pause(0.4)

        before = len(app.runtime.vpn.library)
        await pilot.press("i")
        await pilot.pause(0.8)
        assert len(app.runtime.vpn.library) == before + 2


async def test_browsing_imports_every_selected_blocklist(app, tmp_path, monkeypatch):
    from nextron.utils import filedialog

    first = tmp_path / "ads.hosts"
    second = tmp_path / "trackers.list"
    first.write_text("0.0.0.0 ads.example.com\n", encoding="utf-8")
    second.write_text("metrics.example.com\n", encoding="utf-8")

    monkeypatch.setattr(filedialog, "available", lambda: "zenity")

    async def fake_pick(**kwargs):
        assert kwargs["file_filter"] is filedialog.BLOCKLIST_FILTER
        return filedialog.DialogResult(paths=(first, second))

    monkeypatch.setattr(filedialog, "pick_files", fake_pick)

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("d")
        await pilot.pause(0.4)

        await pilot.press("i")
        await pilot.pause(1.0)
        enabled = app.runtime.config.dns.enabled_blocklists
        assert "ads.hosts" in enabled
        assert "trackers.list" in enabled
        assert app.runtime.state.dns.blocked_domains == 2


async def test_a_failed_chooser_is_reported_not_treated_as_a_cancel(
    app, monkeypatch
):
    from nextron.utils import filedialog

    monkeypatch.setattr(filedialog, "available", lambda: "zenity")
    notified: list[tuple[str, str]] = []

    async def fake_pick(**kwargs):
        return filedialog.DialogResult(error="the file chooser could not reach the desktop session")

    monkeypatch.setattr(filedialog, "pick_files", fake_pick)

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("v")
        await pilot.pause(0.4)

        original = app.notify
        app.notify = lambda message, **kw: notified.append((message, kw.get("severity", "")))  # type: ignore[method-assign]
        try:
            await pilot.press("i")
            await pilot.pause(0.8)
        finally:
            app.notify = original  # type: ignore[method-assign]

    assert any("could not reach the desktop" in message for message, _ in notified)


async def test_mode_select_only_selects_and_never_connects(app, monkeypatch):
    """M is a menu. Connecting is C, and only C."""
    from nextron.core.config import ConfigManager, RoutingMode

    established: list[RoutingMode] = []
    monkeypatch.setattr(app, "establish_mode", established.append)

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("m")
        await pilot.pause(0.4)

        await pilot.press("down")       # Tor Only -> VPN Only
        await pilot.pause(0.2)
        await pilot.press("enter")
        await pilot.pause(0.4)

        assert established == [], "selecting a mode must not start anything"
        assert isinstance(app.screen, DashboardScreen)
        assert app.runtime.config.routing_mode is RoutingMode.VPN_ONLY
        assert app.runtime.state.routing_mode is RoutingMode.VPN_ONLY
        assert ConfigManager().load().routing_mode is RoutingMode.VPN_ONLY
        assert app.runtime.routing.active is False


async def test_connect_uses_the_selected_mode(app, monkeypatch):
    from nextron.core.config import RoutingMode

    requested: list[RoutingMode] = []
    monkeypatch.setattr(app, "establish_mode", requested.append)

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)

        app.runtime.select_mode(RoutingMode.VPN_ONLY)
        await pilot.press("c")
        await pilot.pause(0.4)

    assert requested == [RoutingMode.VPN_ONLY]


async def test_vpn_only_never_starts_tor(app, monkeypatch):
    """One mode means one mode: no Tor daemon behind a VPN-only session."""
    from nextron.core.config import RoutingMode

    app.runtime.vpn.load_library()          # no TUI needed for this one
    started: list[str] = []

    async def spy_start():
        started.append("tor")

    monkeypatch.setattr(app.runtime.tor, "start", spy_start)

    async def fake_vpn_connect(profile=None, *, reason="manual"):
        return app.runtime.vpn.library.all()[0]

    monkeypatch.setattr(app.runtime.vpn, "connect", fake_vpn_connect)
    app.runtime.config.verification.enabled = False
    app.runtime.config.dns.enabled = False

    await app.runtime.routing.establish(RoutingMode.VPN_ONLY)
    assert started == []
    await app.runtime.routing.teardown()


async def test_vpn_library_enter_selects_the_active_profile(app):
    """V is a menu: Enter chooses which profile the next connection uses."""
    from textual.widgets import DataTable

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("v")
        await pilot.pause(0.5)

        table = app.screen.query_one(DataTable)
        table.move_cursor(row=1)
        await pilot.pause(0.2)
        chosen = app.screen._selected()
        assert chosen is not None

        await pilot.press("enter")
        await pilot.pause(0.5)

        assert app.runtime.config.vpn.active_profile == chosen.id
        # And the dashboard shows it immediately, without connecting.
        assert app.runtime.state.vpn.profile_name == chosen.name
        assert app.runtime.vpn.current is None


async def test_the_dashboard_shows_the_selected_profile_while_idle(app):
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.9)

        panel = render_text(
            app.screen.query_one("#panel-vpn").content, width=200
        )
        assert app.runtime.state.vpn.profile_name
        assert app.runtime.state.vpn.profile_name in panel


# -- selection highlight ----------------------------------------------------- #


def _luminance(color) -> float:
    return 0.2126 * color.r + 0.7152 * color.g + 0.0722 * color.b


async def test_selection_highlight_is_dark_everywhere(app):
    """Regression guard: Textual uses ``-highlight``, not ``--highlight``.

    The selector was wrong once, which silently left the bright default cursor
    colour in place while scrolling. Assert the effective colour, not the CSS.
    """
    from textual.widgets import DataTable, ListView

    from nextron.tui.widgets.menu import NavMenu

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.9)

        def highlighted(widget):
            rows = [child for child in widget.children if child.has_class("-highlight")]
            assert rows, f"{type(widget).__name__} has no highlighted row"
            return rows[0].background_colors[1]

        menu = app.screen.query_one(NavMenu)
        menu.focus()
        await pilot.pause(0.3)
        assert _luminance(highlighted(menu)) < 80

        # Scrolling the routing modes -- the case that prompted the change.
        await pilot.press("m")
        await pilot.pause(0.5)
        modes = app.screen.query_one("#mode-list", ListView)
        modes.focus()
        await pilot.pause(0.3)
        for _ in range(3):
            colour = highlighted(modes)
            assert _luminance(colour) < 80, f"mode highlight {colour.hex} is too bright"
            await pilot.press("down")
            await pilot.pause(0.15)
        await pilot.press("escape")
        await pilot.pause(0.3)

        # And the profile table cursor.
        await pilot.press("v")
        await pilot.pause(0.5)
        table = app.screen.query_one(DataTable)
        table.focus()
        await pilot.pause(0.3)
        cursor = table.get_component_styles("datatable--cursor").background
        effective = table.background_colors[1] + cursor
        assert _luminance(effective) < 80


async def test_vpn_library_shows_the_certificate_column(app):
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("v")
        await pilot.pause(0.5)

        from textual.widgets import DataTable

        table = app.screen.query_one(DataTable)
        headers = [str(column.label) for column in table.columns.values()]
        assert "Cert" in headers


def _write_catalogue(*urls: str) -> None:
    """A blocklist catalogue such as a user would drop in themselves."""
    from nextron.dns.sources import CATALOGUE_NAME
    from nextron.storage import paths

    paths.ensure_layout()
    (paths.dns_dir() / CATALOGUE_NAME).write_text(
        "# NEXTRON sources\n" + "".join(f"{url}\n" for url in urls), encoding="utf-8"
    )


async def test_dns_screen_offers_the_catalogue_and_download(app):
    _write_catalogue(
        "https://example.com/ads.txt", "https://example.com/trackers.txt"
    )

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("d")
        await pilot.pause(0.6)

        from textual.widgets import Static

        summary = render_text(
            app.screen.query_one("#dns-summary", Static).content, width=200
        )
        assert "Sources" in summary
        assert "U to download" in summary

        # U asks first: a download is network traffic the user must agree to.
        await pilot.press("u")
        await pilot.pause(0.4)
        assert type(app.screen).__name__ == "ConfirmScreen"
        await pilot.press("n")
        await pilot.pause(0.3)
        assert type(app.screen).__name__ == "DNSShieldScreen"


async def test_downloading_lists_updates_the_shield(app, monkeypatch):
    """The whole update path, with the network stubbed out."""
    import httpx

    from nextron.dns import sources as sources_module

    _write_catalogue("https://example.com/ads.txt")

    list_text = "||ads.example.com^\n||tracker.example.net^\n0.0.0.0 spy.example.org\n"

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, text=list_text)
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(sources_module.httpx, "AsyncClient", factory)

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("d")
        await pilot.pause(0.6)

        await pilot.press("u")
        await pilot.pause(0.4)
        await pilot.press("y")
        for _ in range(40):
            await pilot.pause(0.5)
            if app.runtime.state.dns.blocked_domains:
                break

        assert app.runtime.state.dns.blocked_domains == 3
        assert app.runtime.config.dns.enabled_blocklists
        assert app.runtime.dns.library.is_blocked("ads.example.com")
        assert app.runtime.dns.library.is_blocked("sub.tracker.example.net")
        assert not app.runtime.dns.library.is_blocked("example.com")


# -- a successful connection must land on the dashboard --------------------- #


async def test_a_successful_connect_does_not_cover_the_dashboard(app, monkeypatch):
    """A modal over the dashboard makes every key look dead (real bug)."""
    from nextron.core.config import RoutingMode
    from nextron.core.verification import CheckResult, VerificationReport

    report = VerificationReport(mode=RoutingMode.TOR_ONLY)
    report.checks = [
        CheckResult("Tor bootstrap", True, "100% bootstrapped"),
        CheckResult("DNS leak", False, "a local stub resolver", required=False),
        CheckResult("IPv6 leak", False, "IPv6 escapes", required=False),
    ]
    assert report.passed and report.warnings          # the exact field case

    async def fake_connect(mode=None, **kwargs):
        return report

    monkeypatch.setattr(app.runtime, "connect", fake_connect)

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)

        app.establish_mode(RoutingMode.TOR_ONLY)
        await pilot.pause(1.0)

        assert isinstance(app.screen, DashboardScreen), (
            f"a successful connect left {type(app.screen).__name__} on top"
        )


async def test_a_failed_verification_does_show_the_report(app, monkeypatch):
    from nextron.core.config import RoutingMode
    from nextron.core.verification import CheckResult, VerificationReport

    report = VerificationReport(mode=RoutingMode.TOR_ONLY)
    report.checks = [CheckResult("Routing correctness", False, "not using Tor")]
    assert not report.passed

    async def fake_connect(mode=None, **kwargs):
        return report

    monkeypatch.setattr(app.runtime, "connect", fake_connect)

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)

        app.establish_mode(RoutingMode.TOR_ONLY)
        await pilot.pause(1.0)
        assert type(app.screen).__name__ == "ReportScreen"
        await pilot.press("escape")
        await pilot.pause(0.4)
        assert isinstance(app.screen, DashboardScreen)


async def test_space_reaches_the_rotate_action_from_the_dashboard(app, monkeypatch):
    """Guards the keypress path that the covering modal used to swallow."""
    fired: list[str] = []

    async def fake_rotate():
        fired.append("rotate")
        return True

    monkeypatch.setattr(app.runtime, "rotate_now", fake_rotate)

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("space")
        await pilot.pause(0.5)

    assert fired == ["rotate"]


# -- the quit dialog must be dismissable both ways --------------------------- #


async def test_quit_dialog_focuses_quit_so_enter_exits(app):
    """Enter used to hit "Stay", so the app looked impossible to quit."""
    from textual.widgets import Button

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)

        await pilot.press("q")
        await pilot.pause(0.4)
        focused = [b.id for b in app.screen.query(Button) if b.has_focus]
        assert focused == ["confirm"]

        await pilot.press("enter")
        await pilot.pause(0.6)
        assert app._quitting is True


async def test_quit_dialog_keeps_focus_inside_itself(app):
    """Arrow and tab keys used to move focus onto the dashboard underneath."""
    from textual.widgets import Button

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("q")
        await pilot.pause(0.4)

        for key in ("left", "right", "tab", "shift+tab", "up", "down"):
            await pilot.press(key)
            await pilot.pause(0.1)
            assert type(app.screen).__name__ == "ConfirmScreen"
            assert app.focused in list(app.screen.query(Button)), (
                f"{key} moved focus out of the dialog"
            )

        await pilot.press("escape")
        await pilot.pause(0.3)
        assert isinstance(app.screen, DashboardScreen)
        assert app._quitting is False


async def test_destructive_dialogs_focus_cancel(app):
    """A stray Enter must never delete a profile."""
    from textual.widgets import Button

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("v")
        await pilot.pause(0.5)

        before = len(app.runtime.vpn.library)
        await pilot.press("x")
        await pilot.pause(0.4)
        assert [b.id for b in app.screen.query(Button) if b.has_focus] == ["cancel"]

        await pilot.press("enter")
        await pilot.pause(0.5)
        assert len(app.runtime.vpn.library) == before


async def test_socks_mode_tells_the_user_the_browser_is_not_routed(app, monkeypatch):
    """"Connected" while the browser is untouched is what looked broken."""
    from nextron.core.config import RoutingMode
    from nextron.core.verification import CheckResult, VerificationReport

    report = VerificationReport(mode=RoutingMode.TOR_ONLY)
    report.checks = [CheckResult("Tor bootstrap", True, "100% bootstrapped")]

    async def fake_connect(mode=None, **kwargs):
        return report

    monkeypatch.setattr(app.runtime, "connect", fake_connect)
    notices: list[str] = []

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)

        monkeypatch.setattr(
            app, "notify", lambda message, **kw: notices.append(message)
        )
        app.establish_mode(RoutingMode.TOR_ONLY)
        await pilot.pause(1.0)

    socks_notice = [n for n in notices if "SOCKS5" in n]
    assert socks_notice, f"no SOCKS guidance in {notices}"
    assert "browser is NOT routed" in socks_notice[0]
    assert f"port {app.runtime.tor.socks_port}" in socks_notice[0]


# -- the menu answers to both the mouse and the keyboard --------------------- #


def _menu_item(app, action: str):
    """The NavMenu list item that carries *action*."""
    from nextron.tui.widgets.menu import NavMenu

    menu = app.screen.query_one(NavMenu)
    for item in menu.children:
        entry = NavMenu.entry_of(item)
        if entry is not None and entry.action == action:
            return menu, item
    raise AssertionError(f"no menu entry for {action!r}")


@pytest.mark.parametrize(
    "action,screen_name",
    [
        ("mode_select", "ModeSelectScreen"),
        ("vpn_library", "VPNLibraryScreen"),
        ("dns_shield", "DNSShieldScreen"),
        ("logs", "LogsScreen"),
        ("about", "AboutScreen"),
    ],
)
async def test_clicking_a_menu_entry_opens_its_screen(app, action, screen_name):
    """Tapping an entry must do exactly what its letter does."""
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.7)

        _, item = _menu_item(app, action)
        await pilot.click(item)
        await pilot.pause(0.5)
        assert type(app.screen).__name__ == screen_name

        await pilot.press("escape")
        await pilot.pause(0.3)
        assert isinstance(app.screen, DashboardScreen)


async def test_enter_on_the_highlighted_menu_entry_opens_its_screen(app):
    """Enter used to raise TypeError because NavMenu.Selected shadowed ListView's."""
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.7)

        menu, item = _menu_item(app, "vpn_library")
        menu.focus()
        menu.index = list(menu.children).index(item)
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.5)

        assert type(app.screen).__name__ == "VPNLibraryScreen"


async def test_clicking_a_section_header_does_nothing(app):
    """Headers are disabled; a tap on one must not move the cursor or act."""
    from nextron.tui.widgets.menu import NavMenu

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.7)

        menu = app.screen.query_one(NavMenu)
        header = next(item for item in menu.children if item.disabled)
        before = menu.index

        await pilot.click(header)
        await pilot.pause(0.3)

        assert isinstance(app.screen, DashboardScreen)
        assert menu.index == before


# -- settings are grouped under their own headings --------------------------- #


async def test_settings_are_grouped_under_section_headings(app):
    """Each engine's options sit under its own heading, in one run."""
    from nextron.tui.screens.settings import (
        ROWS,
        SectionHeader,
        Setting,
        SettingsScreen,
    )

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("s")
        await pilot.pause(0.6)
        assert isinstance(app.screen, SettingsScreen)

        # The heading replaces the column that used to repeat on every row.
        table = app.screen.query_one("#settings-table")
        assert [str(c.label) for c in table.columns.values()] == [
            "Setting",
            "Value",
            "Notes",
        ]
        assert table.row_count == len(ROWS)

        headings = [r.name for r in ROWS if isinstance(r, SectionHeader)]
        assert headings == ["Tor", "VPN", "DNS Shield", "Verification", "Interface"]
        assert len(headings) == len(set(headings)), "a section was split in two"

        # Every setting belongs to the heading above it.
        current = None
        for row in ROWS:
            if isinstance(row, SectionHeader):
                current = row.name
                continue
            assert isinstance(row, Setting)
            assert row.section == current, f"{row.label} sits under {current}"


async def test_the_cursor_steps_over_headings(app):
    from nextron.tui.screens.settings import ROWS, SectionHeader

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("s")
        await pilot.pause(0.6)

        table = app.screen.query_one("#settings-table")
        assert not isinstance(ROWS[table.cursor_row], SectionHeader)

        visited = []
        for _ in range(len(ROWS)):
            await pilot.press("down")
            await pilot.pause(0.02)
            visited.append(table.cursor_row)
        assert not [i for i in visited if isinstance(ROWS[i], SectionHeader)]

        for _ in range(len(ROWS)):
            await pilot.press("up")
            await pilot.pause(0.02)
            assert not isinstance(ROWS[table.cursor_row], SectionHeader)

        # And the first setting is still reachable going up.
        assert table.cursor_row == next(
            i for i, r in enumerate(ROWS) if not isinstance(r, SectionHeader)
        )


async def test_enter_on_a_heading_changes_nothing(app):
    from nextron.tui.screens.settings import ROWS, SectionHeader, SettingsScreen

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("s")
        await pilot.pause(0.6)

        before = app.runtime.config.model_dump()
        table = app.screen.query_one("#settings-table")
        table.move_cursor(
            row=next(i for i, r in enumerate(ROWS) if isinstance(r, SectionHeader))
        )
        await pilot.pause(0.2)
        await pilot.press("enter")
        await pilot.pause(0.4)

        assert isinstance(app.screen, SettingsScreen)
        assert app.runtime.config.model_dump() == before


async def test_every_setting_is_still_reachable_and_editable(app):
    """Grouping must not orphan a setting behind a heading."""
    from nextron.tui.screens.settings import ROWS, SETTINGS, Setting, SettingsScreen

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.8)
        await pilot.press("s")
        await pilot.pause(0.6)

        assert [r for r in ROWS if isinstance(r, Setting)] == list(SETTINGS)
        for setting in SETTINGS:
            row = SettingsScreen.row_of(setting.path)
            assert ROWS[row] is setting

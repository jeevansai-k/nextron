"""The native file-chooser integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from nextron.core import constants
from nextron.utils import filedialog as fd


@pytest.fixture
def graphical(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("NEXTRON_NO_FILE_DIALOG", raising=False)


# -- capability detection --------------------------------------------------- #


def test_no_backend_without_a_graphical_session(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert fd.available() is None
    assert "graphical session" in fd.unavailable_reason()


def test_backend_can_be_disabled_by_environment(monkeypatch, graphical):
    monkeypatch.setenv("NEXTRON_NO_FILE_DIALOG", "1")
    assert fd.available() is None
    assert "NEXTRON_NO_FILE_DIALOG" in fd.unavailable_reason()


def test_missing_binaries_are_reported_with_a_fix(monkeypatch, graphical):
    monkeypatch.setattr("nextron.core.process.which", lambda binary: None)
    assert fd.available() is None
    assert "apt install zenity" in fd.unavailable_reason()


def test_backend_preference_order(monkeypatch, graphical):
    monkeypatch.setattr(
        "nextron.core.process.which",
        lambda binary: "/usr/bin/kdialog" if binary == "kdialog" else None,
    )
    assert fd.available() == "kdialog"

    monkeypatch.setattr(
        "nextron.core.process.which",
        lambda binary: f"/usr/bin/{binary}" if binary in ("zenity", "kdialog") else None,
    )
    assert fd.available() == "zenity"


# -- filters ---------------------------------------------------------------- #


def test_vpn_filter_covers_every_supported_suffix():
    for suffix in constants.VPN_IMPORT_SUFFIXES:
        assert f"*{suffix}" in fd.VPN_FILTER.patterns
        assert f"*{suffix.upper()}" in fd.VPN_FILTER.patterns


def test_blocklist_filter_covers_every_supported_suffix():
    for suffix in constants.BLOCKLIST_SUFFIXES:
        assert f"*{suffix}" in fd.BLOCKLIST_FILTER.patterns


def test_filter_syntax_per_backend():
    assert fd.VPN_FILTER.zenity.startswith("VPN profiles | *.ovpn")
    assert fd.VPN_FILTER.kdialog.endswith("|VPN profiles")


# -- command construction --------------------------------------------------- #


def test_zenity_command_requests_multiple_selection(monkeypatch, graphical):
    monkeypatch.setattr("nextron.core.process.which", lambda b: f"/usr/bin/{b}")
    command = fd._build_command(
        "zenity",
        title="Import",
        file_filter=fd.VPN_FILTER,
        multiple=True,
        start_dir=Path("/tmp"),
    )
    assert "--file-selection" in command
    assert "--multiple" in command
    assert "--separator=\n" in command
    assert any(part.startswith("--file-filter=VPN profiles") for part in command)
    assert "--filename=/tmp/" in command


def test_kdialog_command_uses_its_own_syntax(monkeypatch, graphical):
    monkeypatch.setattr("nextron.core.process.which", lambda b: f"/usr/bin/{b}")
    command = fd._build_command(
        "kdialog",
        title="Import",
        file_filter=fd.BLOCKLIST_FILTER,
        multiple=True,
        start_dir=Path("/tmp"),
    )
    assert "--getopenfilename" in command
    assert "--multiple" in command
    assert command[command.index("--getopenfilename") + 1] == "/tmp"


# -- output parsing --------------------------------------------------------- #


def test_newline_separated_output_is_parsed(tmp_path):
    first = tmp_path / "a.ovpn"
    second = tmp_path / "b.ovpn"
    first.write_text("client\n", encoding="utf-8")
    second.write_text("client\n", encoding="utf-8")

    parsed = fd._parse_output("zenity", f"{first}\n{second}\n")
    assert parsed == [first, second]


def test_kdialog_quoted_output_is_parsed(tmp_path):
    path = tmp_path / "list one.hosts"
    path.write_text("0.0.0.0 x.example\n", encoding="utf-8")
    assert fd._parse_output("kdialog", f"'{path}'") == [path]


def test_nonexistent_and_duplicate_entries_are_dropped(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("x.example\n", encoding="utf-8")
    parsed = fd._parse_output("zenity", f"{real}\n{real}\n{tmp_path / 'ghost.txt'}\n")
    assert parsed == [real]


# -- end to end with a stubbed chooser -------------------------------------- #


async def test_selection_is_returned(monkeypatch, graphical, tmp_path):
    chosen = tmp_path / "berlin.ovpn"
    chosen.write_text("client\n", encoding="utf-8")

    monkeypatch.setattr("nextron.core.process.which", lambda b: f"/usr/bin/{b}")

    async def fake_run(*command, **kwargs):
        from nextron.core.process import CommandResult

        return CommandResult(tuple(command), 0, f"{chosen}\n", "")

    monkeypatch.setattr("nextron.core.process.run", fake_run)
    result = await fd.pick_files(file_filter=fd.VPN_FILTER)
    assert list(result) == [chosen]
    assert bool(result) is True
    assert result.error is None


async def test_cancelling_is_not_an_error(monkeypatch, graphical):
    monkeypatch.setattr("nextron.core.process.which", lambda b: f"/usr/bin/{b}")

    async def fake_run(*command, **kwargs):
        from nextron.core.process import CommandResult

        return CommandResult(tuple(command), 1, "", "")

    monkeypatch.setattr("nextron.core.process.run", fake_run)
    result = await fd.pick_files()
    assert result.cancelled is True
    assert result.error is None
    assert not result


async def test_a_chooser_that_cannot_reach_the_display_reports_an_error(
    monkeypatch, graphical
):
    """The common sudo case must be distinguishable from a cancel."""
    monkeypatch.setattr("nextron.core.process.which", lambda b: f"/usr/bin/{b}")

    async def fake_run(*command, **kwargs):
        from nextron.core.process import CommandResult

        return CommandResult(tuple(command), 1, "", "cannot open display: :0")

    monkeypatch.setattr("nextron.core.process.run", fake_run)
    result = await fd.pick_files()
    assert result.cancelled is False
    assert result.error is not None
    assert "sudo" in result.error


async def test_no_backend_returns_a_reason(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    result = await fd.pick_files()
    assert result.error is not None
    assert not result.cancelled
